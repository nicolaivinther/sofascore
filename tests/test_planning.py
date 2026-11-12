"""Unit tests for sofascore.planning (what each page leads to) and the league list."""

# pylint: disable=missing-function-docstring

import json
from datetime import timedelta
from pathlib import Path

from sofascore import planning
from sofascore.leagues import BY_TID, LEAGUES
from sofascore.sports import BASKETBALL

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SPORT = BASKETBALL.code


def _load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_paths_and_keys():
    page = planning.events_page_item(planning.EVENTS_NEXT, SPORT, 264, 100873, 2, 50, True)
    assert page.entity_key == "264|100873|2"
    assert page.url_path == "/unique-tournament/264/season/100873/events/next/2"
    assert planning.page_of(page.entity_key) == (264, 100873, 2)
    assert planning.event_item(SPORT, 7, 264, 1, 10, True).url_path == "/event/7"
    assert planning.team_item(SPORT, 9, 264).url_path == "/team/9"


def test_seasons_lead_to_the_first_page_of_each_season():
    item = planning.seasons_item(SPORT, 264, planning.PRIORITY_BACKFILL)
    out = planning.discover(item, _load("seasons"))
    # Per season: two events pages, three standings tables, the rounds.
    assert len(out) == 6 * 19
    assert {o.entity_type for o in out} == {
        planning.EVENTS_LAST,
        planning.EVENTS_NEXT,
        planning.STANDINGS,
        planning.ROUNDS,
    }
    pages = [o for o in out if o.entity_type in planning.EVENT_PAGES]
    assert all(o.entity_key.endswith("|0") for o in pages)
    assert {o.entity_key for o in out if o.entity_type == planning.STANDINGS and o.season_id == 100873} == {
        "264|100873|total",
        "264|100873|home",
        "264|100873|away",
    }
    assert all(not o.recollect and o.priority == planning.PRIORITY_BACKFILL and o.season_id for o in out)


def test_results_pages_walk_back_once():
    item = planning.events_page_item(planning.EVENTS_LAST, SPORT, 264, 64689, 0, 100, False)
    out = planning.discover(item, _load("events_last_complete"))
    pages = [o for o in out if o.entity_type == planning.EVENTS_LAST]
    assert [p.entity_key for p in pages] == ["264|64689|1"] and not pages[0].recollect
    finished = [e for e in _load("events_last_complete")["events"] if e["status"]["type"] == "finished"]
    stats = [o for o in out if o.entity_type == planning.EVENT_STATISTICS]
    assert sorted(int(o.entity_key) for o in stats) == sorted(e["id"] for e in finished)
    assert {o.entity_type for o in out} >= {planning.TEAM, planning.EVENT_STATISTICS}


def test_schedule_pages_always_re_walk():
    item = planning.events_page_item(planning.EVENTS_NEXT, SPORT, 264, 100873, 0, 50, True)
    pages = [
        o
        for o in planning.discover(item, _load("events_next_current"))
        if o.entity_type == planning.EVENTS_NEXT
    ]
    assert [p.entity_key for p in pages] == ["264|100873|1"] and pages[0].recollect


def test_last_page_has_no_next_page():
    item = planning.events_page_item(planning.EVENTS_LAST, SPORT, 264, 100873, 0, 50, True)
    out = planning.discover(item, _load("events_last_current"))
    assert not [o for o in out if o.entity_type in planning.EVENT_PAGES]


def test_finished_game_queues_statistics_and_lineups_only_with_the_flag():
    event = _load("event")["event"]
    items = planning.event_detail_items(SPORT, event, 14251, 100046, 10, recollect=True)
    assert [(i.entity_type, i.url_path, i.recollect) for i in items] == [
        (planning.EVENT_STATISTICS, "/event/16732145/statistics", True),
        (planning.EVENT_LINEUPS, "/event/16732145/lineups", True),
    ]
    event["hasEventPlayerStatistics"] = False
    assert [i.entity_type for i in planning.event_detail_items(SPORT, event, 14251, 100046, 10)] == [
        planning.EVENT_STATISTICS
    ]
    event["status"] = {"type": "notstarted"}
    assert not planning.event_detail_items(SPORT, event, 14251, 100046, 10)


def test_a_game_leads_to_its_teams():
    item = planning.event_item(SPORT, 16732145, 14251, 100046, 10, True)
    out = planning.discover(item, _load("event"))
    assert [o.entity_key for o in out] == ["331094", "369729"]
    assert all(o.priority == planning.PRIORITY_TEAM for o in out)


def test_refresh_steps_then_freeze():
    assert planning.next_refresh(0) == timedelta(hours=6)
    assert planning.next_refresh(1) == timedelta(hours=36)
    assert planning.next_refresh(2) == timedelta(days=7)
    assert planning.next_refresh(3) is None


def test_league_list():
    assert len(LEAGUES) == 59 and len(BY_TID) == 59
    assert {lg.sport for lg in LEAGUES} == {BASKETBALL}
    assert all(lg.sport.sofascore_sport for lg in LEAGUES)
    assert 28754 not in BY_TID and BY_TID[14251].canonical_league_id == 1143
