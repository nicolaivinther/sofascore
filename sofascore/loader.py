"""Write a stored Sofascore payload into the typed ``sofascore_*`` tables.

The worker calls ``load`` right after a fetch; ``run reparse`` calls it for every row of
``sofascore_raw``, so the typed tables can always be rebuilt without touching Sofascore.

Rules:
- a row whose sport (Sofascore's slug) is not the queue item's sport is skipped and counted, so
  a wrong tid in ``leagues.py`` can never put a football game in the basketball rows;
- a re-dated game keeps its old start in ``previous_start_ts`` and gets ``rescheduled_at``;
- a finished game is never turned back into a fixture by a lagging list page;
- a game's team stats and player box scores, and a season's standings table, are replaced as a
  whole, so a corrected payload never leaves stale rows behind.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from psycopg2.extras import Json, execute_values

from sofascore import parser, planning
from sofascore.sports import get_sport

logger = logging.getLogger(__name__)

EVENT_COLUMNS: tuple[str, ...] = (
    "event_id",
    "tid",
    "season_id",
    "tournament_id",
    "tournament_name",
    "group_name",
    "round",
    "round_name",
    "start_ts",
    "home_team_id",
    "home_team_name",
    "away_team_id",
    "away_team_name",
    "home_score",
    "away_score",
    "home_p1",
    "home_p2",
    "home_p3",
    "home_p4",
    "away_p1",
    "away_p2",
    "away_p3",
    "away_p4",
    "home_normaltime",
    "away_normaltime",
    "home_overtime",
    "away_overtime",
    "home_penalties",
    "away_penalties",
    "status_code",
    "status_type",
    "status_description",
    "winner_code",
    "has_event_player_statistics",
)
_EVENT_UPDATES = ",\n        ".join(f"{col} = EXCLUDED.{col}" for col in EVENT_COLUMNS if col != "event_id")
_UPSERT_EVENTS_SQL = f"""
    INSERT INTO sofascore_events (sport, {", ".join(EVENT_COLUMNS)})
    VALUES %s
    ON CONFLICT (event_id) DO UPDATE SET
        previous_start_ts = CASE WHEN sofascore_events.start_ts <> EXCLUDED.start_ts
                                 THEN sofascore_events.start_ts
                                 ELSE sofascore_events.previous_start_ts END,
        rescheduled_at = CASE WHEN sofascore_events.start_ts <> EXCLUDED.start_ts
                              THEN now() ELSE sofascore_events.rescheduled_at END,
        {_EVENT_UPDATES},
        last_seen_at = now(), removed_at = NULL, updated_at = now()
    WHERE NOT (sofascore_events.status_type = 'finished'
               AND EXCLUDED.status_type IN ('notstarted', 'inprogress'))
"""

_UPSERT_TOURNAMENT_SQL = """
    INSERT INTO sofascore_tournaments
        (tid, sport, name, slug, category_id, category_name, category_alpha2, category_flag,
         gender, tier, has_groups, has_rounds, has_playoff_series, start_ts, end_ts)
    VALUES (%(tid)s, %(sport)s, %(name)s, %(slug)s, %(category_id)s, %(category_name)s,
            %(category_alpha2)s, %(category_flag)s, %(gender)s, %(tier)s, %(has_groups)s,
            %(has_rounds)s, %(has_playoff_series)s, %(start_ts)s, %(end_ts)s)
    ON CONFLICT (tid) DO UPDATE SET
        name = EXCLUDED.name, slug = EXCLUDED.slug, category_id = EXCLUDED.category_id,
        category_name = EXCLUDED.category_name, category_alpha2 = EXCLUDED.category_alpha2,
        category_flag = EXCLUDED.category_flag, gender = EXCLUDED.gender, tier = EXCLUDED.tier,
        has_groups = EXCLUDED.has_groups, has_rounds = EXCLUDED.has_rounds,
        has_playoff_series = EXCLUDED.has_playoff_series, start_ts = EXCLUDED.start_ts,
        end_ts = EXCLUDED.end_ts, updated_at = now()
"""

_UPSERT_SEASONS_SQL = """
    INSERT INTO sofascore_seasons (sport, tid, season_id, name, year)
    VALUES %s
    ON CONFLICT (tid, season_id) DO UPDATE SET
        name = EXCLUDED.name, year = EXCLUDED.year, updated_at = now()
"""

_UPSERT_TEAM_SQL = """
    INSERT INTO sofascore_teams
        (team_id, sport, name, short_name, full_name, slug, name_code, gender, country_alpha2,
         country_name, national, team_type, disabled)
    VALUES (%(team_id)s, %(sport)s, %(name)s, %(short_name)s, %(full_name)s, %(slug)s,
            %(name_code)s, %(gender)s, %(country_alpha2)s, %(country_name)s, %(national)s,
            %(team_type)s, %(disabled)s)
    ON CONFLICT (team_id) DO UPDATE SET
        name = EXCLUDED.name, short_name = EXCLUDED.short_name, full_name = EXCLUDED.full_name,
        slug = EXCLUDED.slug, name_code = EXCLUDED.name_code, gender = EXCLUDED.gender,
        country_alpha2 = EXCLUDED.country_alpha2, country_name = EXCLUDED.country_name,
        national = EXCLUDED.national, team_type = EXCLUDED.team_type,
        disabled = EXCLUDED.disabled, updated_at = now()
"""

_SEASON_ROUNDS_SQL = """
    UPDATE sofascore_seasons SET current_round = %s, rounds = %s, updated_at = now()
    WHERE sport = %s AND tid = %s AND season_id = %s
"""

TEAM_STAT_COLUMNS = (
    "event_id",
    "period",
    "side",
    "stat_key",
    "group_name",
    "stat_name",
    "value",
    "total",
    "display",
)
_DELETE_TEAM_STATS_SQL = "DELETE FROM sofascore_event_team_stats WHERE sport = %s AND event_id = %s"
_INSERT_TEAM_STATS_SQL = f"""
    INSERT INTO sofascore_event_team_stats (sport, {", ".join(TEAM_STAT_COLUMNS)}) VALUES %s
    ON CONFLICT (event_id, period, side, stat_key) DO NOTHING
"""

PLAYER_COLUMNS = (
    "player_id",
    "name",
    "first_name",
    "last_name",
    "short_name",
    "slug",
    "position",
    "jersey_number",
    "height_cm",
    "gender",
    "date_of_birth",
    "country_alpha2",
    "country_name",
)
_UPSERT_PLAYERS_SQL = f"""
    INSERT INTO sofascore_players (sport, {", ".join(PLAYER_COLUMNS)}) VALUES %s
    ON CONFLICT (player_id) DO UPDATE SET
        {", ".join(f"{c} = COALESCE(EXCLUDED.{c}, sofascore_players.{c})" for c in PLAYER_COLUMNS[1:])},
        updated_at = now()
"""

PLAYER_STAT_COLUMNS = (
    "event_id",
    "player_id",
    "side",
    "team_id",
    "shirt_number",
    "jersey_number",
    "position",
    "substitute",
    *parser.PLAYER_STAT_COLUMNS.values(),
    "statistics",
)
_DELETE_PLAYER_STATS_SQL = "DELETE FROM sofascore_event_player_stats WHERE sport = %s AND event_id = %s"
_INSERT_PLAYER_STATS_SQL = f"""
    INSERT INTO sofascore_event_player_stats (sport, {", ".join(PLAYER_STAT_COLUMNS)}) VALUES %s
    ON CONFLICT (event_id, player_id) DO NOTHING
"""

STANDINGS_COLUMNS = (
    "tid",
    "season_id",
    "standings_type",
    "block_id",
    "team_id",
    "block_name",
    "tournament_id",
    "group_name",
    "is_group",
    "position",
    "matches",
    "wins",
    "draws",
    "losses",
    "scores_for",
    "scores_against",
    "points",
    "percentage",
    "games_behind",
    "streak",
    "promotion_id",
    "promotion_text",
    "block_updated_at",
)
_DELETE_STANDINGS_SQL = """
    DELETE FROM sofascore_standings
    WHERE sport = %s AND tid = %s AND season_id = %s AND standings_type = %s
"""
_INSERT_STANDINGS_SQL = f"""
    INSERT INTO sofascore_standings (sport, {", ".join(STANDINGS_COLUMNS)}) VALUES %s
    ON CONFLICT (tid, season_id, standings_type, block_id, team_id) DO NOTHING
"""


@dataclass
class LoadResult:
    """What one payload wrote."""

    rows: int = 0
    skipped_sport: int = 0
    # The ``event`` payload's game, parsed (None for other entity types).
    event: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


def _slug_ok(sport_code: str, thing: dict[str, Any]) -> bool:
    expected = get_sport(sport_code).sofascore_sport
    return parser.sport_slug(thing) == expected


def _upsert_events(cur: Any, sport: str, events: list[dict[str, Any]], result: LoadResult) -> None:
    rows = []
    for event in events:
        if not _slug_ok(sport, event):
            result.skipped_sport += 1
            continue
        row = parser.parse_event(event)
        if row is None:
            continue
        rows.append((sport, *(row[col] for col in EVENT_COLUMNS)))
    if rows:
        execute_values(cur, _UPSERT_EVENTS_SQL, rows)
    result.rows += len(rows)


def load(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-branches
    cur: Any,
    entity_type: str,
    entity_key: str,
    sport: str,
    tid: int | None,
    payload: Any,
) -> LoadResult:
    """Write one payload into its typed table(s)."""
    result = LoadResult()
    if entity_type == planning.TOURNAMENT:
        row = parser.parse_tournament(payload)
        cur.execute(_UPSERT_TOURNAMENT_SQL, {**row, "sport": sport})
        result.rows = 1
    elif entity_type == planning.SEASONS:
        seasons = parser.parse_seasons(payload, int(tid if tid is not None else entity_key))
        if seasons:
            execute_values(
                cur,
                _UPSERT_SEASONS_SQL,
                [(sport, s["tid"], s["season_id"], s["name"], s["year"]) for s in seasons],
            )
        result.rows = len(seasons)
    elif entity_type in planning.EVENT_PAGES:
        _upsert_events(cur, sport, payload.get("events", []), result)
    elif entity_type == planning.EVENT:
        _upsert_events(cur, sport, [payload["event"]], result)
        if result.rows:
            result.event = parser.parse_event(payload["event"])
    elif entity_type == planning.TEAM:
        if _slug_ok(sport, payload["team"]):
            cur.execute(_UPSERT_TEAM_SQL, {**parser.parse_team(payload), "sport": sport})
            result.rows = 1
        else:
            result.skipped_sport = 1
    elif entity_type == planning.EVENT_STATISTICS:
        result.rows = _replace_team_stats(cur, sport, int(entity_key), payload)
    elif entity_type == planning.EVENT_LINEUPS:
        result.rows = _replace_lineups(cur, sport, int(entity_key), payload)
    elif entity_type == planning.STANDINGS:
        tid_s, season_s, kind = entity_key.split("|")
        result.rows = _replace_standings(cur, sport, int(tid_s), int(season_s), kind, payload)
    elif entity_type == planning.ROUNDS:
        tid_s, season_s = entity_key.split("|")
        current, rounds = parser.parse_rounds(payload)
        cur.execute(_SEASON_ROUNDS_SQL, (current, Json(rounds), sport, int(tid_s), int(season_s)))
        result.rows = int(cur.rowcount)
    else:
        raise ValueError(f"unknown entity type {entity_type!r}")
    if result.skipped_sport:
        logger.warning(
            "%s %s: %d row(s) of another sport skipped",
            entity_type,
            entity_key,
            result.skipped_sport,
        )
    return result


def _replace_team_stats(cur: Any, sport: str, event_id: int, payload: Any) -> int:
    rows = parser.parse_statistics(payload, event_id)
    cur.execute(_DELETE_TEAM_STATS_SQL, (sport, event_id))
    if rows:
        execute_values(
            cur, _INSERT_TEAM_STATS_SQL, [(sport, *(r[c] for c in TEAM_STAT_COLUMNS)) for r in rows]
        )
    return len(rows)


def _replace_lineups(cur: Any, sport: str, event_id: int, payload: Any) -> int:
    players, stats = parser.parse_lineups(payload, event_id)
    if players:
        execute_values(cur, _UPSERT_PLAYERS_SQL, [(sport, *(p[c] for c in PLAYER_COLUMNS)) for p in players])
    cur.execute(_DELETE_PLAYER_STATS_SQL, (sport, event_id))
    if stats:
        execute_values(
            cur,
            _INSERT_PLAYER_STATS_SQL,
            [
                (sport, *(Json(r[c]) if c == "statistics" else r[c] for c in PLAYER_STAT_COLUMNS))
                for r in stats
            ],
        )
    return len(stats)


def _replace_standings(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    cur: Any, sport: str, tid: int, season_id: int, kind: str, payload: Any
) -> int:
    rows = parser.parse_standings(payload, tid, season_id, kind)
    cur.execute(_DELETE_STANDINGS_SQL, (sport, tid, season_id, kind))
    if rows:
        execute_values(
            cur, _INSERT_STANDINGS_SQL, [(sport, *(r[c] for c in STANDINGS_COLUMNS)) for r in rows]
        )
    return len(rows)


def mark_event_removed(cur: Any, event_id: int) -> None:
    """``/event/{id}`` 404s: Sofascore dropped the game (a ghost fixture)."""
    cur.execute(
        """
        UPDATE sofascore_events SET removed_at = now(), updated_at = now()
        WHERE event_id = %s AND removed_at IS NULL
        """,
        (event_id,),
    )
