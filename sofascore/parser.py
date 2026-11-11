"""Pure Sofascore JSON -> row functions. No network, no DB.

Rows are dicts keyed by ``sofascore_*`` column names. The payloads themselves stay in
``sofascore_raw``, so a change here is a reparse (``python -m sofascore.run reparse``),
never a re-fetch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# Score keys Sofascore uses per side. ``overtime`` is one aggregate over every overtime period;
# ``penalties`` is a shootout tally, which basketball doesn't have.
PERIOD_KEYS = ("period1", "period2", "period3", "period4")
FINISHED = "finished"


def _ts(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, tz=UTC) if value is not None else None


def _int(side: dict[str, Any], key: str) -> int | None:
    value = side.get(key)
    return int(value) if value is not None else None


def sport_slug(event_or_team: dict[str, Any]) -> str | None:
    """The Sofascore sport slug of an event (via its tournament) or a team."""
    if "sport" in event_or_team:
        return (event_or_team.get("sport") or {}).get("slug")
    category = (event_or_team.get("tournament") or {}).get("category") or {}
    return (category.get("sport") or {}).get("slug")


def parse_tournament(data: dict[str, Any]) -> dict[str, Any]:
    """``/unique-tournament/{tid}`` -> a ``sofascore_tournaments`` row (category as given)."""
    ut = data["uniqueTournament"]
    category = ut.get("category") or {}
    return {
        "tid": int(ut["id"]),
        "name": ut.get("name"),
        "slug": ut.get("slug"),
        "category_id": category.get("id"),
        "category_name": category.get("name"),
        "category_alpha2": category.get("alpha2"),
        "category_flag": category.get("flag"),
        "gender": ut.get("gender"),
        "tier": ut.get("tier"),
        "has_groups": ut.get("hasGroups"),
        "has_rounds": ut.get("hasRounds"),
        "has_playoff_series": ut.get("hasPlayoffSeries"),
        "start_ts": _ts(ut.get("startDateTimestamp")),
        "end_ts": _ts(ut.get("endDateTimestamp")),
    }


def parse_seasons(data: dict[str, Any], tid: int) -> list[dict[str, Any]]:
    """``/unique-tournament/{tid}/seasons`` -> ``sofascore_seasons`` rows, newest first."""
    return [
        {
            "tid": tid,
            "season_id": int(s["id"]),
            "name": s.get("name"),
            "year": s.get("year"),
        }
        for s in data.get("seasons", [])
    ]


def parse_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """One API event -> a ``sofascore_events`` row, or None without a unique tournament."""
    tournament = event.get("tournament") or {}
    unique_tournament = tournament.get("uniqueTournament")
    if not unique_tournament:
        return None
    home = event.get("homeTeam") or {}
    away = event.get("awayTeam") or {}
    home_score = event.get("homeScore") or {}
    away_score = event.get("awayScore") or {}
    status = event.get("status") or {}
    round_info = event.get("roundInfo") or {}
    row: dict[str, Any] = {
        "event_id": int(event["id"]),
        "tid": int(unique_tournament["id"]),
        "season_id": (event.get("season") or {}).get("id"),
        "tournament_id": tournament.get("id"),
        "tournament_name": tournament.get("name"),
        "group_name": tournament.get("groupName"),
        "round": round_info.get("round"),
        "round_name": round_info.get("name"),
        "start_ts": _ts(event["startTimestamp"]),
        "home_team_id": home.get("id"),
        "home_team_name": home.get("name"),
        "away_team_id": away.get("id"),
        "away_team_name": away.get("name"),
        "home_score": _int(home_score, "current"),
        "away_score": _int(away_score, "current"),
        "status_code": status.get("code"),
        "status_type": status.get("type"),
        "status_description": status.get("description"),
        "winner_code": event.get("winnerCode"),
        "has_event_player_statistics": event.get("hasEventPlayerStatistics"),
    }
    for side, score in (("home", home_score), ("away", away_score)):
        for number, key in enumerate(PERIOD_KEYS, start=1):
            row[f"{side}_p{number}"] = _int(score, key)
        row[f"{side}_normaltime"] = _int(score, "normaltime")
        row[f"{side}_overtime"] = _int(score, "overtime")
        row[f"{side}_penalties"] = _int(score, "penalties")
    return row


def parse_team(data: dict[str, Any]) -> dict[str, Any]:
    """``/team/{id}`` -> a ``sofascore_teams`` row (country as given)."""
    team = data["team"]
    country = team.get("country") or {}
    return {
        "team_id": int(team["id"]),
        "name": team.get("name"),
        "short_name": team.get("shortName"),
        "full_name": team.get("fullName"),
        "slug": team.get("slug"),
        "name_code": team.get("nameCode"),
        "gender": team.get("gender"),
        "country_alpha2": country.get("alpha2"),
        "country_name": country.get("name"),
        "national": team.get("national"),
        "team_type": team.get("type"),
        "disabled": team.get("disabled"),
    }


def event_team_ids(data: dict[str, Any]) -> list[int]:
    """Team ids in an events page or an ``/event/{id}`` payload, in order, deduped."""
    events = data.get("events") or ([data["event"]] if "event" in data else [])
    seen: dict[int, None] = {}
    for event in events:
        for side in ("homeTeam", "awayTeam"):
            team_id = (event.get(side) or {}).get("id")
            if team_id is not None:
                seen.setdefault(int(team_id), None)
    return list(seen)


def parse_statistics(data: dict[str, Any], event_id: int) -> list[dict[str, Any]]:
    """``/event/{id}/statistics`` -> ``sofascore_event_team_stats`` rows (both sides, every period)."""
    rows: list[dict[str, Any]] = []
    for period in data.get("statistics", []):
        for group in period.get("groups", []):
            for item in group.get("statisticsItems", []):
                for side in ("home", "away"):
                    rows.append(
                        {
                            "event_id": event_id,
                            "period": period.get("period"),
                            "side": side,
                            "stat_key": item.get("key") or item.get("name"),
                            "group_name": group.get("groupName"),
                            "stat_name": item.get("name"),
                            "value": item.get(f"{side}Value"),
                            "total": item.get(f"{side}Total"),
                            "display": item.get(side),
                        }
                    )
    return rows


# lineups ``statistics`` key -> sofascore_event_player_stats column (the rest stays in the jsonb).
PLAYER_STAT_COLUMNS: dict[str, str] = {
    "secondsPlayed": "seconds_played",
    "points": "points",
    "rebounds": "rebounds",
    "offensiveRebounds": "offensive_rebounds",
    "defensiveRebounds": "defensive_rebounds",
    "assists": "assists",
    "steals": "steals",
    "blocks": "blocks",
    "turnovers": "turnovers",
    "personalFouls": "personal_fouls",
    "plusMinus": "plus_minus",
    "fieldGoalsMade": "field_goals_made",
    "fieldGoalAttempts": "field_goal_attempts",
    "twoPointsMade": "two_points_made",
    "twoPointAttempts": "two_point_attempts",
    "threePointsMade": "three_points_made",
    "threePointAttempts": "three_point_attempts",
    "freeThrowsMade": "free_throws_made",
    "freeThrowAttempts": "free_throw_attempts",
}


def _date(ts: int | None) -> Any | None:
    return _ts(ts).date() if ts is not None else None  # type: ignore[union-attr]


def parse_player(player: dict[str, Any]) -> dict[str, Any]:
    """A lineup's ``player`` object -> a ``sofascore_players`` row."""
    country = player.get("country") or {}
    return {
        "player_id": int(player["id"]),
        "name": player.get("name"),
        "first_name": player.get("firstName"),
        "last_name": player.get("lastName"),
        "short_name": player.get("shortName"),
        "slug": player.get("slug"),
        "position": player.get("position"),
        "jersey_number": player.get("jerseyNumber"),
        "height_cm": player.get("height"),
        "gender": player.get("gender"),
        "date_of_birth": _date(player.get("dateOfBirthTimestamp")),
        "country_alpha2": country.get("alpha2"),
        "country_name": country.get("name"),
    }


def parse_lineups(data: dict[str, Any], event_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``/event/{id}/lineups`` -> (``sofascore_players`` rows, ``sofascore_event_player_stats`` rows)."""
    players: dict[int, dict[str, Any]] = {}
    stats: list[dict[str, Any]] = []
    for side in ("home", "away"):
        for entry in (data.get(side) or {}).get("players", []):
            player = entry.get("player") or {}
            if player.get("id") is None:
                continue
            row = parse_player(player)
            players[row["player_id"]] = row
            values = entry.get("statistics") or {}
            stat_row: dict[str, Any] = {
                "event_id": event_id,
                "player_id": row["player_id"],
                "side": side,
                "team_id": entry.get("teamId"),
                "shirt_number": entry.get("shirtNumber"),
                "jersey_number": entry.get("jerseyNumber"),
                "position": entry.get("position"),
                "substitute": entry.get("substitute"),
                "statistics": values,
            }
            for key, column in PLAYER_STAT_COLUMNS.items():
                stat_row[column] = values.get(key)
            stats.append(stat_row)
    return list(players.values()), stats


def parse_standings(
    data: dict[str, Any], tid: int, season_id: int, standings_type: str
) -> list[dict[str, Any]]:
    """``.../standings/{total|home|away}`` -> ``sofascore_standings`` rows, every block kept."""
    rows: list[dict[str, Any]] = []
    for block in data.get("standings", []):
        tournament = block.get("tournament") or {}
        for row in block.get("rows", []):
            promotion = row.get("promotion") or {}
            team = row.get("team") or {}
            if team.get("id") is None:
                continue
            rows.append(
                {
                    "tid": tid,
                    "season_id": season_id,
                    "standings_type": standings_type,
                    "block_id": block.get("id"),
                    "team_id": team["id"],
                    "block_name": block.get("name"),
                    "tournament_id": tournament.get("id"),
                    "group_name": tournament.get("groupName"),
                    "is_group": tournament.get("isGroup"),
                    "position": row.get("position"),
                    "matches": row.get("matches"),
                    "wins": row.get("wins"),
                    "draws": row.get("draws"),
                    "losses": row.get("losses"),
                    "scores_for": row.get("scoresFor"),
                    "scores_against": row.get("scoresAgainst"),
                    "points": row.get("points"),
                    "percentage": row.get("percentage"),
                    "games_behind": row.get("gamesBehind"),
                    "streak": row.get("streak"),
                    "promotion_id": promotion.get("id"),
                    "promotion_text": promotion.get("text"),
                    "block_updated_at": _ts(block.get("updatedAtTimestamp")),
                }
            )
    return rows


def parse_rounds(data: dict[str, Any]) -> tuple[int | None, list[dict[str, Any]]]:
    """``.../rounds`` -> (current round, the rounds as given)."""
    current = (data.get("currentRound") or {}).get("round")
    return current, list(data.get("rounds") or [])
