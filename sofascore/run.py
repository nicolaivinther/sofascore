"""Sofascore collector: planners add rows to ``sofascore_queue``, one worker drains it.

    python -m sofascore.run backfill [--tid 14251 ...]   # seed every season, then work
    python -m sofascore.run daily                        # re-queue open seasons, then work
    python -m sofascore.run fast                         # just-played games, then work
    python -m sofascore.run work [--max-minutes 60]      # drain the queue only
    python -m sofascore.run reparse                      # rebuild typed tables from raw
    python -m sofascore.run status                       # queue + coverage summary
    python -m sofascore.run captcha                      # pass the check by hand, fetch one page

Every command takes ``--dry-run`` (plan and report; write and fetch nothing), ``--sport`` and
``--tid``. The rules (fetch once when done, re-check +6h/+36h/+7d, freeze) are in ARCHITECTURE.md.

Exit codes: 0 done (or cooling down after a block), 2 Sofascore refused us and the run stopped,
3 too many items failed in a row and the run stopped.
"""

from __future__ import annotations

import argparse
import fcntl
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sofascore import loader, planning, store
from sofascore.captcha import BrowserSession
from sofascore.client import DEFAULT_BASE_URL, SofascoreBlockedError, SofascoreClient, SofascoreHTTPError
from sofascore.config.config import Config
from sofascore.config.logging import setup_logger
from sofascore.config.sentry_config import sentry_report_error, setup_sentry
from sofascore.database.connection import connect
from sofascore.leagues import LEAGUES, SofascoreLeague
from sofascore.sports import SPORT_CODES, get_sport
from sofascore.transport import CAPTCHA_HEADER

logger = setup_logger("sofascore")

COLLECTOR_VERSION = "0.1.0"
MAX_ATTEMPTS = 5
EXIT_BLOCKED = 2
EXIT_FAILING = 3
# This many items failing one after another stops the run: something is wrong beyond one page.
MAX_CONSECUTIVE_ERRORS = 20
# A season is frozen once its newest game is this old and nothing is scheduled or live.
SEASON_GRACE_DAYS = 14
# The fast run looks at games that started at most this long ago; older ones are the daily's.
FAST_MAX_AGE_HOURS = 48
# Fixtures not listed for this long are reported as possible ghosts.
GHOST_HOURS = 36
# One collector at a time (cron or by hand), so Sofascore never sees two clients at once.
LOCK_PATH = "/tmp/sofascore_run.lock"  # nosec B108 — a lock file, no data
# Worker time budget per command, minutes. At 3-5 s a request a daily pass (a few hundred mostly
# 304s) takes 20-40 minutes; a full backfill runs for days and can be resumed at any point.
BUDGET_MINUTES = {"fast": 25.0, "daily": 90.0, "backfill": 4320.0, "work": 4320.0}
# A backfill waits out a block (SOFASCORE_COOLDOWN_MIN) at most this often per run; the cron runs
# stop at the first one.
MAX_COOLDOWNS = {"backfill": 3, "work": 3}
BLOCK_MARKER = "sofascore_blocked_until"
BROWSER_PROFILES = "sofascore_browser_profiles"
# How long `captcha` waits for a person to pass the check, and the page it then fetches.
MANUAL_SOLVE_TIMEOUT_S = 300
CHECK_PATH = "/unique-tournament/264/seasons"

# A season is complete when it is not its tournament's newest season, nothing in it is scheduled
# or live, its newest game is past the grace period, and its schedule page 404s.
_FREEZE_SEASONS_SQL = """
    UPDATE sofascore_seasons s SET is_complete = true, completed_at = now(), updated_at = now()
    WHERE s.sport = ANY(%(sports)s) AND NOT s.is_complete AND s.tid = ANY(%(tids)s)
      AND s.season_id <> (SELECT MAX(n.season_id) FROM sofascore_seasons n WHERE n.tid = s.tid)
      AND NOT EXISTS (
          SELECT 1 FROM sofascore_events e
          WHERE e.tid = s.tid AND e.season_id = s.season_id
            AND e.status_type IN ('notstarted', 'inprogress') AND e.removed_at IS NULL)
      AND COALESCE((SELECT MAX(e.start_ts) FROM sofascore_events e
                    WHERE e.tid = s.tid AND e.season_id = s.season_id), '-infinity')
          < now() - make_interval(days => %(grace)s)
      AND EXISTS (
          SELECT 1 FROM sofascore_queue q
          WHERE q.entity_type = 'events_next'
            AND q.entity_key = s.tid || '|' || s.season_id || '|0'
            AND q.status = 'none_available')
    RETURNING s.tid, s.season_id
"""

_OPEN_SEASONS_SQL = """
    SELECT tid, season_id, sport FROM sofascore_seasons
    WHERE sport = ANY(%(sports)s) AND tid = ANY(%(tids)s) AND NOT is_complete
"""

_FAST_GAMES_SQL = """
    SELECT event_id, tid, season_id FROM sofascore_events
    WHERE sport = %(sport)s AND tid = ANY(%(tids)s)
      AND status_type IN ('notstarted', 'inprogress') AND removed_at IS NULL
      AND start_ts <= now() - make_interval(mins => %(after)s)
      AND start_ts >= now() - make_interval(hours => %(max_age)s)
"""

_GHOSTS_SQL = """
    SELECT COUNT(*) FROM sofascore_events
    WHERE sport = ANY(%(sports)s) AND status_type = 'notstarted' AND removed_at IS NULL
      AND start_ts > now() AND last_seen_at < now() - make_interval(hours => %(hours)s)
"""

_COVERAGE_SQL = """
    SELECT tid, tournament, COUNT(*), SUM(games), SUM(finished), SUM(open_games),
           SUM(with_team_stats), SUM(flagged_player_stats), SUM(with_player_stats),
           SUM(standings_blocks), SUM(queue_pending), SUM(queue_failed)
    FROM v_sofascore_coverage
    WHERE sport = ANY(%(sports)s)
    GROUP BY tid, tournament ORDER BY SUM(games) DESC NULLS LAST
"""

# Lists before details, so a game's detail payload is the last word on it.
_REPARSE_SQL = """
    SELECT r.entity_type, r.entity_key, r.sport, q.tid, r.payload
    FROM sofascore_raw r
    LEFT JOIN sofascore_queue q USING (entity_type, entity_key)
    WHERE r.sport = ANY(%(sports)s)
    ORDER BY array_position(%(order)s, r.entity_type), r.fetched_at
"""
_REPARSE_ORDER = [
    planning.TOURNAMENT,
    planning.SEASONS,
    planning.ROUNDS,
    planning.EVENTS_LAST,
    planning.EVENTS_NEXT,
    planning.EVENT,
    planning.EVENT_STATISTICS,
    planning.EVENT_LINEUPS,
    planning.STANDINGS,
    planning.TEAM,
]


@dataclass
class WorkStats:
    """What one worker run did (logged at the end, so over-fetching shows)."""

    counts: Counter = field(default_factory=Counter)
    rows: int = 0

    def summary(self) -> str:
        """One log line."""
        parts = ", ".join(f"{key} {n}" for key, n in sorted(self.counts.items()))
        return f"{parts or 'nothing to do'}; rows written {self.rows}"


def _leagues(sports: Sequence[str], tids: Sequence[int] | None) -> list[SofascoreLeague]:
    chosen = [lg for lg in LEAGUES if lg.sport.code in sports and (not tids or lg.tid in tids)]
    unknown = set(tids or ()) - {lg.tid for lg in LEAGUES}
    if unknown:
        raise SystemExit(f"tid(s) not in sofascore/leagues.py: {sorted(unknown)}")
    return chosen


def plan_backfill(leagues: Iterable[SofascoreLeague]) -> list[planning.QueueItem]:
    """Tournament + season list of every league; discovery queues the rest (all seasons)."""
    items = []
    for league in leagues:
        sport = league.sport.code
        items.append(planning.tournament_item(sport, league.tid, planning.PRIORITY_BACKFILL))
        items.append(planning.seasons_item(sport, league.tid, planning.PRIORITY_BACKFILL))
    return items


def plan_daily(
    cur: Any, leagues: Sequence[SofascoreLeague], sports: Sequence[str]
) -> list[planning.QueueItem]:
    """Freeze finished seasons, then re-queue every league's tournament and season list and, per
    open season, the newest results page, the first schedule page (which re-walks the whole
    published schedule), the standings and the rounds.
    """
    tids = [lg.tid for lg in leagues]
    cur.execute(_FREEZE_SEASONS_SQL, {"sports": list(sports), "tids": tids, "grace": SEASON_GRACE_DAYS})
    for tid, season_id in cur.fetchall():
        logger.info("season %s/%s complete: frozen", tid, season_id)
    items = []
    for league in leagues:
        sport, prio = league.sport.code, planning.PRIORITY_DAILY
        items.append(planning.tournament_item(sport, league.tid, prio, recollect=True))
        items.append(planning.seasons_item(sport, league.tid, prio, recollect=True))
    cur.execute(_OPEN_SEASONS_SQL, {"sports": list(sports), "tids": tids})
    for tid, season_id, sport in cur.fetchall():
        items += planning.season_items(sport, tid, season_id, planning.PRIORITY_DAILY, True)
    return items


def plan_fast(
    cur: Any, leagues: Sequence[SofascoreLeague], sports: Sequence[str]
) -> list[planning.QueueItem]:
    """Games that should be over by now but are not finished in our rows: one ``event`` each."""
    items = []
    for code in sports:
        sport = get_sport(code)
        tids = [lg.tid for lg in leagues if lg.sport.code == code]
        if not tids:
            continue
        cur.execute(
            _FAST_GAMES_SQL,
            {"sport": code, "tids": tids, "after": sport.result_after_minutes, "max_age": FAST_MAX_AGE_HOURS},
        )
        for event_id, tid, season_id in cur.fetchall():
            items.append(planning.event_item(code, event_id, tid, season_id, planning.PRIORITY_FAST, True))
    return items


def _after_event(cur: Any, item: store.Claimed, payload: Any, finished: bool, changed: bool) -> bool:
    """For a finished game: queue its statistics and lineups (again, if a correction re-check found
    a changed payload) and schedule the next re-check, or freeze it. True if it did (the item is
    then not marked parsed).
    """
    if not finished:
        return False
    event = payload["event"]
    recollect = changed and item.refresh_step > 0
    store.seed(
        cur,
        planning.event_detail_items(item.sport, event, item.tid, item.season_id, item.priority, recollect),
    )
    delay = planning.next_refresh(item.refresh_step)
    if delay is None:
        store.freeze(cur, item)
    else:
        store.schedule_recheck(cur, item, delay.total_seconds() / 3600, item.refresh_step + 1)
    return True


def _is_finished(loaded: loader.LoadResult) -> bool:
    return bool(loaded.event and loaded.event.get("status_type") == "finished")


def process(cur: Any, client: SofascoreClient, item: store.Claimed, stats: WorkStats) -> None:
    """Fetch one claimed item, store and load it, queue what it leads to."""
    result = client.get(item.url_path, item.etag)
    if result.not_found:
        if item.entity_type == planning.EVENT:
            loader.mark_event_removed(cur, int(item.entity_key))
        store.mark_none_available(cur, item)
        stats.counts["none_available"] += 1
        return
    if result.unchanged:
        payload = store.load_raw(cur, item.entity_type, item.entity_key)
        if payload is not None:
            stats.counts["not_modified"] += 1
            store.seed(cur, planning.discover(item, payload))
            finished = False
            if item.entity_type == planning.EVENT:
                finished = _is_finished(
                    loader.load(cur, item.entity_type, item.entity_key, item.sport, item.tid, payload)
                )
            if not _after_event(cur, item, payload, finished, changed=False):
                store.mark_parsed(cur, item, result.etag)
            return
        result = client.get(item.url_path)  # raw row lost: fetch it whole
    payload = result.data
    if payload is None:
        raise SofascoreHTTPError(f"{item.url_path}: HTTP {result.status} without a body")
    changed = store.store_raw(cur, item, payload, result.etag, COLLECTOR_VERSION)
    loaded = loader.load(cur, item.entity_type, item.entity_key, item.sport, item.tid, payload)
    stats.rows += loaded.rows
    stats.counts["fetched"] += 1
    store.seed(cur, planning.discover(item, payload))
    if not _after_event(cur, item, payload, _is_finished(loaded), changed):
        store.mark_parsed(cur, item)


class CollectorFailingError(RuntimeError):
    """Too many items failed in a row; the run stops instead of burning through the queue."""


@dataclass
class BlockPolicy:
    """What a run does when Sofascore refuses a request (403/429).

    Every refusal writes ``marker`` (blocked until now + ``cooldown_minutes``), which every run
    checks before it starts, so neither cron nor a manual run asks Sofascore again too soon. A
    run with ``max_cooldowns`` waits the cooldown out and carries on; a refusal on the first
    request after a cooldown, or one cooldown too many, stops the run.
    """

    cooldown_minutes: float
    max_cooldowns: int = 0
    marker: Path | None = None
    sleep: Callable[[float], None] = time.sleep

    def blocked_until(self) -> datetime | None:
        """The end of the current cooldown, or None."""
        if self.marker is None or not self.marker.exists():
            return None
        until = datetime.fromisoformat(self.marker.read_text(encoding="utf-8").strip())
        return until if until > datetime.now(UTC) else None

    def record_block(self) -> datetime:
        """Write the marker; returns the end of the cooldown."""
        until = datetime.now(UTC) + timedelta(minutes=self.cooldown_minutes)
        if self.marker is not None:
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            self.marker.write_text(until.isoformat(), encoding="utf-8")
        return until


def work(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches
    conn: Any,
    client: SofascoreClient,
    sports: Sequence[str],
    max_minutes: float,
    max_items: int | None = None,
    max_priority: int | None = None,
    block_policy: BlockPolicy | None = None,
) -> WorkStats:
    """Drain the queue: one item per transaction, until it is empty or a limit is hit."""
    policy = block_policy or BlockPolicy(cooldown_minutes=0)
    cooldowns, just_cooled = 0, False
    stats = WorkStats()
    deadline = time.monotonic() + max_minutes * 60
    with conn.cursor() as cur:
        stuck = store.reset_stuck(cur, sports)
    conn.commit()
    if stuck:
        logger.info("%d stuck in_progress item(s) back to pending", stuck)
    done, consecutive_errors = 0, 0
    while time.monotonic() < deadline and (max_items is None or done < max_items):
        with conn.cursor() as cur:
            item = store.claim(cur, sports, max_priority)
            if item is None:
                conn.commit()
                break
            try:
                process(cur, client, item, stats)
                conn.commit()
                just_cooled, consecutive_errors = False, 0
            except SofascoreBlockedError as exc:
                conn.rollback()
                with conn.cursor() as cur2:
                    store.release(cur2, item)
                conn.commit()
                until = policy.record_block()
                stats.counts["blocked"] += 1
                if just_cooled or cooldowns >= policy.max_cooldowns:
                    raise
                cooldowns, just_cooled = cooldowns + 1, True
                logger.warning(
                    "%s; nothing is asked until %s (cooldown %d)", exc, until.isoformat(), cooldowns
                )
                policy.sleep(policy.cooldown_minutes * 60)
                deadline += policy.cooldown_minutes * 60
                continue
            except Exception as exc:  # pylint: disable=broad-except
                conn.rollback()
                with conn.cursor() as cur2:
                    status = store.fail(cur2, item, f"{type(exc).__name__}: {exc}", MAX_ATTEMPTS)
                conn.commit()
                stats.counts["errors"] += 1
                log = logger.error if status == "failed" else logger.warning
                log("%s %s: %s (%s)", item.entity_type, item.entity_key, exc, status)
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    raise CollectorFailingError(
                        f"{consecutive_errors} items failed in a row; last: {type(exc).__name__}: {exc}"
                    ) from exc
        done += 1
    stats.counts["requests"] = client.request_count
    stats.counts["captcha_solves"] = client.solve_count
    stats.counts["browser_requests"] = client.browser_request_count
    return stats


def reparse(conn: Any, sports: Sequence[str]) -> int:
    """Rebuild the typed tables from ``sofascore_raw`` (no HTTP). Returns payloads loaded."""
    loaded = 0
    with conn.cursor(name="sofascore_reparse") as read_cur, conn.cursor() as cur:
        read_cur.itersize = 200
        read_cur.execute(_REPARSE_SQL, {"sports": list(sports), "order": _REPARSE_ORDER})
        for entity_type, entity_key, sport, tid, blob in read_cur:
            loader.load(cur, entity_type, entity_key, sport, tid, store.decompress(blob))
            loaded += 1
    conn.commit()
    return loaded


def print_status(cur: Any, sports: Sequence[str]) -> None:
    """Queue counts per entity type, ghosts, and coverage per tournament."""
    print("Queue (entity_type: status counts)")
    for entity_type, by_status in store.counts(cur, sports).items():
        print(f"  {entity_type}: " + ", ".join(f"{s} {n}" for s, n in by_status.items()))
    cur.execute(_GHOSTS_SQL, {"sports": list(sports), "hours": GHOST_HOURS})
    print(f"Fixtures not listed for {GHOST_HOURS} h (possible ghosts): {cur.fetchone()[0]}")
    print(
        "Coverage (tid | tournament | seasons | games | finished | open | with team stats | "
        "flagged player stats | with player stats | standings tables | queue pending | queue failed)"
    )
    cur.execute(_COVERAGE_SQL, {"sports": list(sports)})
    for row in cur.fetchall():
        print("  " + " | ".join("" if v is None else str(v) for v in row))


def _seed(conn: Any, items: list[planning.QueueItem], dry_run: bool) -> None:
    by_type = Counter(item.entity_type for item in items)
    logger.info("planned %d item(s): %s", len(items), dict(by_type))
    if dry_run:
        conn.rollback()
        return
    with conn.cursor() as cur:
        added = store.seed(cur, items)
    conn.commit()
    logger.info("queued or re-queued %d", added)


def _plan(conn: Any, command: str, leagues: list[SofascoreLeague], sports: list[str], dry_run: bool) -> None:
    with conn.cursor() as cur:
        if command == "backfill":
            _seed(conn, plan_backfill(leagues), dry_run)
        elif command == "daily":
            _seed(conn, plan_daily(cur, leagues, sports), dry_run)
        elif command == "fast":
            _seed(conn, plan_fast(cur, leagues, sports), dry_run)


def _collect(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    conn: Any,
    client: SofascoreClient,
    sports: Sequence[str],
    max_minutes: float,
    max_items: int | None,
    max_priority: int | None,
    policy: BlockPolicy,
) -> WorkStats:
    """Run the worker and always close the client (and its browser) afterwards."""
    try:
        return work(conn, client, sports, max_minutes, max_items, max_priority, policy)
    finally:
        client.close()


def build_client(config: Config, state_dir: Path) -> SofascoreClient:
    """The collector's client: curl first, the browser for challenges, the token kept on disk."""
    browser = BrowserSession(
        state_dir / BROWSER_PROFILES,
        channel=config.SOFASCORE_BROWSER_CHANNEL,
    )
    return SofascoreClient(
        browser=browser,
        pause_seconds=(config.SOFASCORE_PAUSE_MIN_S, config.SOFASCORE_PAUSE_MAX_S),
    )


def check_by_hand(config: Config, state_dir: Path, dry_run: bool) -> int:
    """Open the captcha page in a Chrome window for a person to pass (no auto-click), then fetch
    one page from it: shows whether a solved browser gets data from this machine.
    """
    if dry_run:
        return 0
    browser = BrowserSession(
        state_dir / BROWSER_PROFILES,
        channel=config.SOFASCORE_BROWSER_CHANNEL,
        auto_click=False,
        solve_timeout_s=MANUAL_SOLVE_TIMEOUT_S,
    )
    try:
        token = browser.solve()
        logger.info("captcha passed; token valid until %s", _utc(token.expires_at))
        response = browser.get(f"{DEFAULT_BASE_URL}{CHECK_PATH}", {CAPTCHA_HEADER: token.value})
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("captcha check failed: %s", exc)
        return EXIT_BLOCKED
    finally:
        browser.close()
    logger.info("%s from the solved browser: HTTP %d", CHECK_PATH, response.status)
    return 0 if response.status == 200 else EXIT_BLOCKED


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


def main(  # pylint: disable=too-many-return-statements,too-many-locals
    argv: Sequence[str] | None = None,
) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument(
        "command", choices=("backfill", "daily", "fast", "work", "reparse", "status", "captcha")
    )
    parser.add_argument("--sport", choices=SPORT_CODES, action="append", help="default: all")
    parser.add_argument("--tid", type=int, action="append", help="limit to these tournaments")
    parser.add_argument("--max-minutes", type=float, default=None, help="worker time budget")
    parser.add_argument("--max-items", type=int, default=None, help="worker item budget")
    parser.add_argument("--dry-run", action="store_true", help="plan and report only")
    args = parser.parse_args(argv)
    config = Config()
    logger.setLevel(config.LOGLEVEL.upper())
    setup_sentry()

    state_dir = Path(config.SOFASCORE_STATE_DIR)
    if args.command == "captcha":
        return check_by_hand(config, state_dir, args.dry_run)
    sports = sorted(set(args.sport or [lg.sport.code for lg in LEAGUES]))
    leagues = _leagues(sports, args.tid)
    conn = connect(config)
    try:
        if args.command == "status":
            conn.set_session(readonly=True)
            with conn.cursor() as cur:
                print_status(cur, sports)
            return 0
        if args.command == "reparse":
            if args.dry_run:
                logger.info("dry run: would reload every sofascore_raw row of %s", sports)
                return 0
            logger.info("reparse: %d payload(s) loaded", reparse(conn, sports))
            return 0
        # Held open until the process exits, which releases the lock.
        lock = open(LOCK_PATH, "w", encoding="utf-8")  # pylint: disable=consider-using-with
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("another Sofascore run holds %s; exiting", LOCK_PATH)
            return 0
        policy = BlockPolicy(
            cooldown_minutes=config.SOFASCORE_COOLDOWN_MIN,
            max_cooldowns=MAX_COOLDOWNS.get(args.command, 0),
            marker=state_dir / BLOCK_MARKER,
        )
        until = policy.blocked_until()
        if until is not None:
            logger.info(
                "Sofascore refused us earlier; cooling down until %s, not asking now", until.isoformat()
            )
            return 0
        _plan(conn, args.command, leagues, sports, args.dry_run)
        if args.dry_run:
            return 0
        # The fast run does only its own games (and the teams/stats they bring); the backfill's
        # items wait for a daily or backfill run.
        max_priority = planning.PRIORITY_TEAM if args.command == "fast" else None
        budget = args.max_minutes or BUDGET_MINUTES[args.command]
        stats = _collect(
            conn, build_client(config, state_dir), sports, budget, args.max_items, max_priority, policy
        )
        logger.info("%s: %s", args.command, stats.summary())
        return 0
    except SofascoreBlockedError as exc:
        logger.error("Sofascore is blocking the client: %s", exc)
        sentry_report_error(exc, extra={"job": "sofascore", "command": args.command})
        return EXIT_BLOCKED
    except CollectorFailingError as exc:
        logger.error("Sofascore run stopped: %s", exc)
        sentry_report_error(exc, extra={"job": "sofascore", "command": args.command})
        return EXIT_FAILING
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
