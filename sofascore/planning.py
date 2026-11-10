"""Queue items, their API paths, and what each fetched page leads to. Pure functions.

Entity types and keys (``sofascore_queue.entity_type`` / ``entity_key``):

==================  ======================  ==================================================
entity_type         entity_key              path
==================  ======================  ==================================================
tournament          ``tid``                 /unique-tournament/{tid}
seasons             ``tid``                 /unique-tournament/{tid}/seasons
events_last         ``tid|sid|page``        /unique-tournament/{tid}/season/{sid}/events/last/{p}
events_next         ``tid|sid|page``        /unique-tournament/{tid}/season/{sid}/events/next/{p}
event               ``event_id``            /event/{id}
event_statistics    ``event_id``            /event/{id}/statistics
event_lineups       ``event_id``            /event/{id}/lineups
standings           ``tid|sid|type``        /unique-tournament/{tid}/season/{sid}/standings/{type}
rounds              ``tid|sid``             /unique-tournament/{tid}/season/{sid}/rounds
team                ``team_id``             /team/{id}
==================  ======================  ==================================================

A finished game queues its team statistics (they exist even where Sofascore has no player
stats) and, when ``hasEventPlayerStatistics`` is set, its lineups (players + box scores).

``events_last`` page 0 is the newest finished page; ``hasNextPage`` leads to older ones.
``events_next`` page 0 is the next upcoming page; ``hasNextPage`` leads further ahead, so walking
it to the end gives the full published schedule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sofascore import parser

TOURNAMENT = "tournament"
SEASONS = "seasons"
EVENTS_LAST = "events_last"
EVENTS_NEXT = "events_next"
EVENT = "event"
EVENT_STATISTICS = "event_statistics"
EVENT_LINEUPS = "event_lineups"
STANDINGS = "standings"
ROUNDS = "rounds"
TEAM = "team"
EVENT_PAGES = (EVENTS_LAST, EVENTS_NEXT)
STANDINGS_TYPES = ("total", "home", "away")

# Lower runs first. The fast run's games jump the queue; a backfill never blocks them.
PRIORITY_FAST = 10
PRIORITY_DAILY = 50
PRIORITY_TEAM = 70
PRIORITY_BACKFILL = 100

# A finished game is re-fetched this long after each check for late corrections, then frozen.
REFRESH_STEPS: tuple[timedelta, ...] = (
    timedelta(hours=6),
    timedelta(hours=36),
    timedelta(days=7),
)


@dataclass(frozen=True)
class QueueItem:  # pylint: disable=too-many-instance-attributes
    """One API path to fetch."""

    entity_type: str
    entity_key: str
    url_path: str
    sport: str
    priority: int
    tid: int | None = None
    season_id: int | None = None
    # True: re-queue it even if it was fetched before (a schedule or a live game can change).
    recollect: bool = False


def tournament_item(sport: str, tid: int, priority: int, recollect: bool = False) -> QueueItem:
    """The tournament detail page."""
    return QueueItem(
        TOURNAMENT,
        str(tid),
        f"/unique-tournament/{tid}",
        sport,
        priority,
        tid,
        None,
        recollect,
    )


def seasons_item(sport: str, tid: int, priority: int, recollect: bool = False) -> QueueItem:
    """The tournament's season list."""
    return QueueItem(
        SEASONS,
        str(tid),
        f"/unique-tournament/{tid}/seasons",
        sport,
        priority,
        tid,
        None,
        recollect,
    )


def events_page_item(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    kind: str,
    sport: str,
    tid: int,
    season_id: int,
    page: int,
    priority: int,
    recollect: bool,
) -> QueueItem:
    """One ``events/last`` or ``events/next`` page of a season."""
    direction = "last" if kind == EVENTS_LAST else "next"
    return QueueItem(
        kind,
        f"{tid}|{season_id}|{page}",
        f"/unique-tournament/{tid}/season/{season_id}/events/{direction}/{page}",
        sport,
        priority,
        tid,
        season_id,
        recollect,
    )


def event_item(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    sport: str,
    event_id: int,
    tid: int | None,
    season_id: int | None,
    priority: int,
    recollect: bool,
) -> QueueItem:
    """One game's detail page (its result)."""
    return QueueItem(
        EVENT,
        str(event_id),
        f"/event/{event_id}",
        sport,
        priority,
        tid,
        season_id,
        recollect,
    )


def team_item(sport: str, team_id: int, tid: int | None) -> QueueItem:
    """A team's detail page; fetched once."""
    return QueueItem(TEAM, str(team_id), f"/team/{team_id}", sport, PRIORITY_TEAM, tid)


def event_detail_items(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    sport: str,
    event: dict[str, Any],
    tid: int | None,
    season_id: int | None,
    priority: int,
    recollect: bool = False,
) -> list[QueueItem]:
    """A finished game's team statistics, and its lineups when Sofascore has player stats."""
    if (event.get("status") or {}).get("type") != parser.FINISHED:
        return []
    event_id = int(event["id"])
    out = [
        QueueItem(
            EVENT_STATISTICS,
            str(event_id),
            f"/event/{event_id}/statistics",
            sport,
            priority,
            tid,
            season_id,
            recollect,
        )
    ]
    if event.get("hasEventPlayerStatistics"):
        out.append(
            QueueItem(
                EVENT_LINEUPS,
                str(event_id),
                f"/event/{event_id}/lineups",
                sport,
                priority,
                tid,
                season_id,
                recollect,
            )
        )
    return out


def season_items(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    sport: str, tid: int, season_id: int, priority: int, recollect: bool, include_pages: bool = True
) -> list[QueueItem]:
    """A season's first results and schedule page, its three standings tables and its rounds."""
    out: list[QueueItem] = []
    if include_pages:
        out += [events_page_item(kind, sport, tid, season_id, 0, priority, recollect) for kind in EVENT_PAGES]
    base = f"/unique-tournament/{tid}/season/{season_id}"
    out += [
        QueueItem(
            STANDINGS,
            f"{tid}|{season_id}|{kind}",
            f"{base}/standings/{kind}",
            sport,
            priority,
            tid,
            season_id,
            recollect,
        )
        for kind in STANDINGS_TYPES
    ]
    out.append(
        QueueItem(ROUNDS, f"{tid}|{season_id}", f"{base}/rounds", sport, priority, tid, season_id, recollect)
    )
    return out


def page_of(entity_key: str) -> tuple[int, int, int]:
    """``tid|sid|page`` -> (tid, season_id, page)."""
    tid, season_id, page = entity_key.split("|")
    return int(tid), int(season_id), int(page)


def discover(item: QueueItem, payload: dict[str, Any]) -> list[QueueItem]:
    """What a fetched page leads to. Seeding ignores items that are already queued, except
    ``recollect`` ones.

    - seasons: per season the first ``events_last`` and ``events_next`` page, the standings and
      the rounds;
    - an events page with ``hasNextPage``: the following page. A schedule (``events_next``) page
      always re-queues the next one, so re-fetching page 0 re-walks the whole published schedule;
      older results pages are fetched once;
    - events pages and games: every team seen, and each finished game's statistics/lineups.
    """
    out: list[QueueItem] = []
    if item.entity_type == SEASONS and item.tid is not None:
        for season in parser.parse_seasons(payload, item.tid):
            out += season_items(item.sport, item.tid, season["season_id"], item.priority, False)
    if item.entity_type in EVENT_PAGES and payload.get("hasNextPage"):
        tid, season_id, page = page_of(item.entity_key)
        out.append(
            events_page_item(
                item.entity_type,
                item.sport,
                tid,
                season_id,
                page + 1,
                item.priority,
                item.entity_type == EVENTS_NEXT,
            )
        )
    if item.entity_type in (*EVENT_PAGES, EVENT):
        out.extend(team_item(item.sport, team_id, item.tid) for team_id in parser.event_team_ids(payload))
    if item.entity_type in EVENT_PAGES:
        for event in payload.get("events", []):
            event_season = (event.get("season") or {}).get("id") or item.season_id
            out += event_detail_items(item.sport, event, item.tid, event_season, item.priority)
    return out


def next_refresh(step: int) -> timedelta | None:
    """Delay before re-check number ``step`` (0-based) of a finished game; None = freeze it."""
    return REFRESH_STEPS[step] if step < len(REFRESH_STEPS) else None
