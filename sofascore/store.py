"""``sofascore_queue`` and ``sofascore_raw`` operations, after the Elite Prospects queue.

Every function takes an open cursor; the caller owns the transaction. Status flow of an item:
``pending -> in_progress -> collected -> parsed``, or ``none_available`` (404), or ``failed``
after ``max_attempts``. A ``parsed`` item comes back only when a planner re-queues it
(``recollect``) or its ``refresh_after`` re-check is due; a ``frozen`` one never does.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from psycopg2.extras import execute_values

from sofascore.planning import QueueItem

_SEED_SQL = """
    INSERT INTO sofascore_queue
        (entity_type, entity_key, url_path, sport, tid, season_id, priority)
    VALUES %s
    ON CONFLICT (entity_type, entity_key) DO NOTHING
    RETURNING 1
"""

# Re-queue what was fetched before, unless it is frozen or already waiting (a pending item, such as
# a game waiting for its correction re-check, keeps its time). Priority only ever goes up.
_RECOLLECT_SQL = """
    INSERT INTO sofascore_queue
        (entity_type, entity_key, url_path, sport, tid, season_id, priority)
    VALUES %s
    ON CONFLICT (entity_type, entity_key) DO UPDATE SET
        status = 'pending', attempts = 0, last_error = NULL, claimed_at = NULL,
        refresh_after = NULL,
        priority = LEAST(sofascore_queue.priority, EXCLUDED.priority),
        tid = COALESCE(EXCLUDED.tid, sofascore_queue.tid),
        season_id = COALESCE(EXCLUDED.season_id, sofascore_queue.season_id)
    WHERE sofascore_queue.frozen_at IS NULL
      AND sofascore_queue.status IN ('collected', 'parsed', 'failed', 'none_available')
    RETURNING 1
"""

_CLAIM_SQL = """
    UPDATE sofascore_queue q SET status = 'in_progress', claimed_at = now()
    FROM (
        SELECT entity_type, entity_key FROM sofascore_queue
        WHERE sport = ANY(%(sports)s) AND status = 'pending'
          AND (refresh_after IS NULL OR refresh_after <= now())
          AND (%(max_priority)s::int IS NULL OR priority <= %(max_priority)s::int)
        ORDER BY priority, refresh_after NULLS FIRST, created_at, entity_key
        LIMIT 1 FOR UPDATE SKIP LOCKED
    ) next
    WHERE q.entity_type = next.entity_type AND q.entity_key = next.entity_key
    RETURNING q.entity_type, q.entity_key, q.url_path, q.sport, q.priority, q.tid, q.season_id,
              q.etag, q.refresh_step
"""


@dataclass(frozen=True)
class Claimed(QueueItem):
    """A queue item handed to the worker, with its stored ETag and re-check step."""

    etag: str | None = None
    refresh_step: int = 0


def seed(cur: Any, items: Iterable[QueueItem]) -> int:
    """Queue items. New ones are inserted; a ``recollect`` item that was fetched before goes back
    to pending. Returns the rows inserted or re-queued.
    """
    fresh: dict[tuple[str, str], tuple] = {}
    again: dict[tuple[str, str], tuple] = {}
    for item in items:
        row = (
            item.entity_type,
            item.entity_key,
            item.url_path,
            item.sport,
            item.tid,
            item.season_id,
            item.priority,
        )
        (again if item.recollect else fresh)[(item.entity_type, item.entity_key)] = row
    total = 0
    for sql, rows in ((_SEED_SQL, fresh), (_RECOLLECT_SQL, again)):
        if rows:
            total += len(execute_values(cur, sql, list(rows.values()), fetch=True))
    return total


def claim(cur: Any, sports: Sequence[str], max_priority: int | None = None) -> Claimed | None:
    """Take the next due pending item (lowest priority first) and mark it in_progress."""
    cur.execute(_CLAIM_SQL, {"sports": list(sports), "max_priority": max_priority})
    row = cur.fetchone()
    if not row:
        return None
    entity_type, entity_key, url_path, sport, priority, tid, season_id, etag, step = row
    return Claimed(
        entity_type,
        entity_key,
        url_path,
        sport,
        priority,
        tid,
        season_id,
        etag=etag,
        refresh_step=step,
    )


def reset_stuck(cur: Any, sports: Sequence[str], older_than_minutes: int = 30) -> int:
    """in_progress items claimed long ago (a killed run) go back to pending."""
    cur.execute(
        """
        UPDATE sofascore_queue SET status = 'pending', claimed_at = NULL
        WHERE sport = ANY(%s) AND status = 'in_progress'
          AND claimed_at < now() - make_interval(mins => %s)
        """,
        (list(sports), older_than_minutes),
    )
    return int(cur.rowcount)


def compress(payload: Any) -> tuple[bytes, str]:
    """gzip'd canonical JSON and the sha256 of the uncompressed JSON."""
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return gzip.compress(data, mtime=0), hashlib.sha256(data).hexdigest()


def decompress(blob: Any) -> Any:
    """Inverse of ``compress``."""
    return json.loads(gzip.decompress(bytes(blob)))


def store_raw(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    cur: Any,
    item: Claimed,
    payload: Any,
    etag: str | None,
    collector_version: str | None,
) -> bool:
    """Upsert the payload and mark the item collected. True if the payload changed."""
    blob, sha = compress(payload)
    cur.execute(
        """
        SELECT payload_sha256 FROM sofascore_raw WHERE entity_type = %s AND entity_key = %s
        """,
        (item.entity_type, item.entity_key),
    )
    previous = cur.fetchone()
    cur.execute(
        """
        INSERT INTO sofascore_raw
            (entity_type, entity_key, sport, payload, payload_sha256, fetched_at, collector_version)
        VALUES (%s, %s, %s, %s, %s, now(), %s)
        ON CONFLICT (entity_type, entity_key) DO UPDATE SET
            payload = EXCLUDED.payload, payload_sha256 = EXCLUDED.payload_sha256,
            fetched_at = now(), collector_version = EXCLUDED.collector_version
        """,
        (item.entity_type, item.entity_key, item.sport, blob, sha, collector_version),
    )
    cur.execute(
        """
        UPDATE sofascore_queue
        SET status = 'collected', collected_at = now(), last_error = NULL, etag = %s
        WHERE entity_type = %s AND entity_key = %s
        """,
        (etag, item.entity_type, item.entity_key),
    )
    return previous is None or previous[0] != sha


def load_raw(cur: Any, entity_type: str, entity_key: str) -> Any | None:
    """The stored payload of one item, or None."""
    cur.execute(
        """
        SELECT payload FROM sofascore_raw WHERE entity_type = %s AND entity_key = %s
        """,
        (entity_type, entity_key),
    )
    row = cur.fetchone()
    return decompress(row[0]) if row else None


def mark_parsed(cur: Any, item: Claimed, etag: str | None = None) -> None:
    """The item's payload is loaded (or a 304 confirmed the stored one)."""
    cur.execute(
        """
        UPDATE sofascore_queue
        SET status = 'parsed', parsed_at = now(), last_error = NULL, attempts = 0,
            refresh_after = NULL, etag = COALESCE(%s, etag)
        WHERE entity_type = %s AND entity_key = %s
        """,
        (etag, item.entity_type, item.entity_key),
    )


def mark_none_available(cur: Any, item: Claimed) -> None:
    """404: Sofascore has nothing at this path. Not retried unless a planner re-queues it."""
    cur.execute(
        """
        UPDATE sofascore_queue
        SET status = 'none_available', parsed_at = now(), last_error = NULL, attempts = 0,
            refresh_after = NULL
        WHERE entity_type = %s AND entity_key = %s
        """,
        (item.entity_type, item.entity_key),
    )


def fail(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    cur: Any,
    item: Claimed,
    error: str,
    max_attempts: int,
    retry_after_minutes: int = 10,
) -> str:
    """Count a failed attempt: back to pending (after a pause) until ``max_attempts``, then
    ``failed``. Returns the new status.
    """
    cur.execute(
        """
        UPDATE sofascore_queue SET attempts = attempts + 1, last_error = %s, claimed_at = NULL,
            status = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'pending' END,
            refresh_after = now() + make_interval(mins => %s)
        WHERE entity_type = %s AND entity_key = %s
        RETURNING status
        """,
        (
            error[:2000],
            max_attempts,
            retry_after_minutes,
            item.entity_type,
            item.entity_key,
        ),
    )
    row = cur.fetchone()
    return str(row[0]) if row else "unknown"


def schedule_recheck(cur: Any, item: Claimed, delay_hours: float, step: int) -> None:
    """Re-fetch a finished game after ``delay_hours`` (correction re-check number ``step``)."""
    cur.execute(
        """
        UPDATE sofascore_queue
        SET status = 'pending', refresh_step = %s, claimed_at = NULL,
            refresh_after = now() + make_interval(secs => %s)
        WHERE entity_type = %s AND entity_key = %s
        """,
        (step, delay_hours * 3600, item.entity_type, item.entity_key),
    )


def freeze(cur: Any, item: Claimed) -> None:
    """Never queue this item again (a finished game past its last re-check)."""
    cur.execute(
        """
        UPDATE sofascore_queue SET status = 'parsed', frozen_at = now(), refresh_after = NULL
        WHERE entity_type = %s AND entity_key = %s
        """,
        (item.entity_type, item.entity_key),
    )


def counts(cur: Any, sports: Sequence[str]) -> dict[str, dict[str, int]]:
    """{entity_type: {status: n}} for ``run status``."""
    cur.execute(
        """
        SELECT entity_type, status, COUNT(*) FROM sofascore_queue
        WHERE sport = ANY(%s) GROUP BY 1, 2 ORDER BY 1, 2
        """,
        (list(sports),),
    )
    out: dict[str, dict[str, int]] = {}
    for entity_type, status, n in cur.fetchall():
        out.setdefault(entity_type, {})[status] = int(n)
    return out
