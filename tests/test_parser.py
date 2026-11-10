"""Unit tests for sofascore.parser, on saved Sofascore payloads."""

# pylint: disable=missing-function-docstring

import json
from datetime import UTC, datetime
from pathlib import Path

from sofascore import parser

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_basketball_event_with_group_and_quarters():
    row = parser.parse_event(_load("event")["event"])
    assert row["event_id"] == 16732145 and row["tid"] == 14251 and row["season_id"] == 100046
    assert row["tournament_id"] == 75778 and row["group_name"] == "Group B"
    assert row["start_ts"] == datetime.fromtimestamp(1793552400, tz=UTC)
    assert (row["home_score"], row["away_score"]) == (84, 97)
    assert [row[f"home_p{n}"] for n in range(1, 5)] == [23, 20, 21, 20]
    assert [row[f"away_p{n}"] for n in range(1, 5)] == [20, 27, 21, 29]
    assert row["home_normaltime"] == 84 and row["home_overtime"] is None
    assert row["status_type"] == "finished" and row["winner_code"] == 2
    assert row["has_event_player_statistics"] is True


def test_hockey_event_has_three_periods_and_no_fourth():
    row = parser.parse_event(_load("hockey_event")["event"])
    assert [row[f"home_p{n}"] for n in range(1, 4)] == [2, 3, 0]
    assert row["home_p4"] is None and row["home_penalties"] is None
    assert parser.sport_slug(_load("hockey_event")["event"]) == "ice-hockey"


def test_overtime_and_shootout_keys():
    event = _load("event")["event"]
    event["homeScore"] = {
        "current": 95,
        "normaltime": 89,
        "overtime": 6,
        "penalties": 3,
    }
    row = parser.parse_event(event)
    assert (row["home_score"], row["home_normaltime"], row["home_overtime"]) == (
        95,
        89,
        6,
    )
    assert row["home_penalties"] == 3


def test_event_without_unique_tournament_is_dropped():
    event = _load("event")["event"]
    event["tournament"].pop("uniqueTournament")
    assert parser.parse_event(event) is None


def test_events_page_and_team_ids():
    page = _load("events_last_complete")
    rows = parser.parse_events(page)
    assert len(rows) == len(page["events"]) and all(r["tid"] == 264 for r in rows)
    ids = parser.event_team_ids(page)
    assert len(ids) == len(set(ids)) and ids
    assert parser.event_team_ids(_load("event")) == [331094, 369729]


def test_team():
    row = parser.parse_team(_load("team"))
    assert row["team_id"] == 331094 and row["short_name"] == "Fabriano"
    assert (row["country_alpha2"], row["gender"], row["national"]) == ("IT", "M", False)
    assert parser.sport_slug(_load("team")["team"]) == "basketball"


def test_tournament_and_seasons():
    tournament = parser.parse_tournament(_load("unique_tournament"))
    assert tournament["tid"] == 264
    seasons = parser.parse_seasons(_load("seasons"), 264)
    assert seasons[0] == {
        "tid": 264,
        "season_id": 100873,
        "name": seasons[0]["name"],
        "year": seasons[0]["year"],
    }
    assert len(seasons) == 19


def test_team_statistics_rows():
    rows = parser.parse_statistics(_load("statistics"), 16732145)
    assert len(rows) == 2 * 6
    free_throws = [r for r in rows if r["stat_key"] == "freeThrowsScored"]
    assert {(r["side"], r["value"], r["total"], r["display"]) for r in free_throws} == {
        ("home", 19, 25, "19/25 (76%)"),
        ("away", 21, 26, "21/26 (80%)"),
    }
    rebounds = next(r for r in rows if r["stat_key"] == "rebounds" and r["side"] == "home")
    assert (rebounds["period"], rebounds["group_name"], rebounds["value"], rebounds["total"]) == (
        "ALL",
        "Other",
        31,
        None,
    )


def test_lineups_give_players_and_box_scores():
    players, stats = parser.parse_lineups(_load("lineups"), 16732145)
    assert [p["player_id"] for p in players] == [1654322, 1645510, 1645616, 1635537]
    gnecchi = next(p for p in players if p["player_id"] == 1645510)
    assert (gnecchi["position"], gnecchi["height_cm"], str(gnecchi["date_of_birth"])) == (
        "G",
        192,
        "1999-11-13",
    )
    row = next(r for r in stats if r["player_id"] == 1645510)
    assert (row["side"], row["points"], row["assists"], row["seconds_played"], row["plus_minus"]) == (
        "home",
        8,
        3,
        1678,
        -28,
    )
    assert row["statistics"]["fieldGoalPct"] == 33
    assert {r["side"] for r in stats} == {"home", "away"}


def test_standings_keep_every_group_block():
    rows = parser.parse_standings(_load("standings_total"), 14251, 100046, "total")
    assert len(rows) == 4
    assert {(r["block_id"], r["group_name"]) for r in rows} == {(298007, "Group A"), (298010, "Group B")}
    top = next(r for r in rows if r["team_id"] == 801048)
    assert (top["position"], top["wins"], top["losses"], top["points"], top["promotion_text"]) == (
        1,
        7,
        0,
        14,
        "Playoffs",
    )
    assert top["is_group"] is True and top["standings_type"] == "total"


def test_rounds():
    assert parser.parse_rounds(_load("rounds")) == (8, [{"round": n} for n in range(1, 5)])
