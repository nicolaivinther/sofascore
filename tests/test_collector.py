"""The collector against a throwaway local Postgres (``conftest.py``).

Real migrations, real queue SQL, real worker; only Sofascore is faked: a client that serves the
saved payloads in ``tests/fixtures`` and 404s everything else.
"""

# pylint: disable=missing-function-docstring

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sofascore import planning, run, store
from sofascore.client import FetchResult, SofascoreBlockedError
from sofascore.leagues import SofascoreLeague
from sofascore.sports import BASKETBALL

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ACB = SofascoreLeague(264, BASKETBALL, 1, "Liga ACB")
SERIE_B = SofascoreLeague(14251, BASKETBALL, 1143, "Serie B")
SPORTS = [BASKETBALL.code]
CURRENT, OLD = 100873, 64689  # ACB seasons in the fixtures


def _load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeClient:
    """Serves ``routes`` (path -> payload, or an int status); 404 for anything else."""

    def __init__(self, routes):
        self.routes = routes  # a reference: tests add routes as the "site" changes
        self.calls: list[tuple[str, str | None]] = []
        self.request_count = 0
        self.solve_count = 0
        self.browser_request_count = 0
        self.unchanged: set[str] = set()

    def get(self, path, etag=None):
        self.calls.append((path, etag))
        self.request_count += 1
        route = self.routes.get(path, 404)
        if route == 403:
            raise SofascoreBlockedError("blocked")
        if isinstance(route, int):
            return FetchResult(status=route)
        if etag and path in self.unchanged:
            return FetchResult(status=304, etag=etag)
        return FetchResult(status=200, data=copy.deepcopy(route), etag=f'W/"{path}"')


def _acb_routes():
    seasons = _load("seasons")
    seasons["seasons"] = [s for s in seasons["seasons"] if s["id"] in (CURRENT, OLD)]
    last_current = _load("events_last_current")
    last_current["hasNextPage"] = False
    next_current = _load("events_next_current")
    next_current["hasNextPage"] = False
    last_old = _load("events_last_complete")
    last_old["hasNextPage"] = False
    base = "/unique-tournament/264"
    return {
        base: _load("unique_tournament"),
        f"{base}/seasons": seasons,
        f"{base}/season/{CURRENT}/events/last/0": last_current,
        f"{base}/season/{CURRENT}/events/next/0": next_current,
        f"{base}/season/{OLD}/events/last/0": last_old,
    }


def _q(conn, sql, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    conn.commit()
    return rows


def _seed(conn, items):
    with conn.cursor() as cur:
        store.seed(cur, items)
    conn.commit()


def _work(conn, client, max_priority=None):
    return run.work(conn, client, SPORTS, max_minutes=1, max_priority=max_priority)


def _backfill(conn, client, leagues=(ACB,)):
    _seed(conn, run.plan_backfill(leagues))
    return _work(conn, client)


def _daily(conn, client, leagues=(ACB,)):
    with conn.cursor() as cur:
        items = run.plan_daily(cur, list(leagues), SPORTS)
    conn.commit()
    _seed(conn, items)
    return _work(conn, client)


def _status(conn, entity_type, entity_key):
    return _q(
        conn,
        "SELECT status, refresh_step, frozen_at IS NOT NULL FROM sofascore_queue "
        "WHERE entity_type = %s AND entity_key = %s",
        entity_type,
        entity_key,
    )[0]


def test_backfill_fills_every_table_and_the_queue(conn):
    client = FakeClient(_acb_routes())
    stats = _backfill(conn, client)

    assert _q(conn, "SELECT tid, sport FROM sofascore_tournaments") == [(264, "basketball")]
    assert _q(conn, "SELECT COUNT(*) FROM sofascore_seasons")[0][0] == 2
    events = _q(
        conn,
        "SELECT season_id, status_type, COUNT(*) FROM sofascore_events GROUP BY 1, 2",
    )
    assert (OLD, "finished", 3) in events and (CURRENT, "notstarted", 2) in events
    # Every page and team was fetched once; the old season has no schedule (404).
    assert _status(conn, planning.EVENTS_NEXT, f"264|{OLD}|0")[0] == "none_available"
    assert _q(conn, "SELECT COUNT(*) FROM sofascore_queue WHERE status = 'pending'")[0][0] == 0
    teams = _q(conn, "SELECT COUNT(*) FROM sofascore_queue WHERE entity_type = 'team'")[0][0]
    assert 0 < teams <= stats.counts["none_available"]
    assert _q(conn, "SELECT COUNT(*) FROM sofascore_raw")[0][0] == stats.counts["fetched"]
    coverage = _q(
        conn,
        "SELECT season_id, games, finished, open_games FROM v_sofascore_coverage ORDER BY 1",
    )
    assert (OLD, 3, 3, 0) in coverage


def test_daily_uses_etags_and_still_re_walks_the_schedule(conn):
    routes = _acb_routes()
    next0 = f"/unique-tournament/264/season/{CURRENT}/events/next/0"
    next1 = f"/unique-tournament/264/season/{CURRENT}/events/next/1"
    routes[next0]["hasNextPage"] = True
    routes[next1] = {"events": [], "hasNextPage": False}
    client = FakeClient(routes)
    _backfill(conn, client)

    client.calls.clear()
    client.unchanged = set(routes)
    stats = _daily(conn, client)
    paths = [path for path, _ in client.calls]
    # Open season only: the old season's pages are not asked again.
    assert next0 in paths and next1 in paths
    assert not any(f"/season/{OLD}/" in p for p in paths)
    assert all(etag for path, etag in client.calls if path in routes)
    assert stats.counts["not_modified"] >= 4 and stats.counts.get("fetched", 0) == 0


def test_lagging_page_never_reverts_a_finished_game_and_re_dates_are_kept(conn):
    routes = _acb_routes()
    client = FakeClient(routes)
    _backfill(conn, client)
    next0 = f"/unique-tournament/264/season/{CURRENT}/events/next/0"
    game = routes[next0]["events"][0]
    old_start = game["startTimestamp"]

    # Sofascore re-dates the game by a day.
    routes[next0]["events"][0]["startTimestamp"] = old_start + 86400
    _daily(conn, client)
    row = _q(
        conn,
        "SELECT start_ts, previous_start_ts, rescheduled_at IS NOT NULL "
        "FROM sofascore_events WHERE event_id = %s",
        game["id"],
    )[0]
    assert row[1] == datetime.fromtimestamp(old_start, tz=UTC) and row[2]

    # It is played; then a stale schedule page still lists it as not started.
    finished = copy.deepcopy(routes[next0]["events"][0])
    finished.update(
        status={"code": 100, "description": "Ended", "type": "finished"},
        homeScore={"current": 80},
        awayScore={"current": 70},
    )
    routes[f"/event/{game['id']}"] = {"event": finished}
    _seed(conn, [planning.event_item("basketball", game["id"], 264, CURRENT, 10, True)])
    _work(conn, client)
    _daily(conn, client)
    assert _q(
        conn,
        "SELECT status_type, home_score FROM sofascore_events WHERE event_id = %s",
        game["id"],
    ) == [("finished", 80)]


def test_fast_run_finishes_a_game_then_re_checks_and_freezes_it(conn):
    event = _load("event")
    started = datetime.now(UTC) - timedelta(hours=3)
    event["event"]["startTimestamp"] = int(started.timestamp())
    fixture = copy.deepcopy(event)
    fixture["event"]["status"] = {
        "code": 0,
        "description": "Not started",
        "type": "notstarted",
    }
    fixture["event"]["homeScore"] = fixture["event"]["awayScore"] = {}
    sb = "/unique-tournament/14251"
    routes = {
        sb: {"uniqueTournament": {"id": 14251, "name": "Serie B"}},
        f"{sb}/seasons": {"seasons": [{"id": 100046, "name": "Serie B 26/27", "year": "26/27"}]},
        f"{sb}/season/100046/events/next/0": {
            "events": [fixture["event"]],
            "hasNextPage": False,
        },
    }
    client = FakeClient(routes)
    _backfill(conn, client, (SERIE_B,))
    key = str(event["event"]["id"])
    assert _q(conn, "SELECT status_type FROM sofascore_events")[0][0] == "notstarted"

    routes[f"/event/{key}"] = event
    with conn.cursor() as cur:
        items = run.plan_fast(cur, [SERIE_B], SPORTS)
    assert [i.entity_key for i in items] == [key]
    _seed(conn, items)
    _work(conn, client, planning.PRIORITY_TEAM)
    assert _q(conn, "SELECT status_type, home_score FROM sofascore_events") == [("finished", 84)]
    assert _status(conn, planning.EVENT, key) == ("pending", 1, False)

    # Nothing is asked again before the re-check is due, and a finished game is not re-planned.
    client.calls.clear()
    _work(conn, client)
    with conn.cursor() as cur:
        assert not run.plan_fast(cur, [SERIE_B], SPORTS)
    assert not client.calls

    for step in (2, 3):
        _q(
            conn,
            "UPDATE sofascore_queue SET refresh_after = now() WHERE entity_key = %s RETURNING 1",
            key,
        )
        _work(conn, client)
        assert _status(conn, planning.EVENT, key) == ("pending", step, False)
    _q(
        conn,
        "UPDATE sofascore_queue SET refresh_after = now() WHERE entity_key = %s RETURNING 1",
        key,
    )
    _work(conn, client)
    assert _status(conn, planning.EVENT, key) == ("parsed", 3, True)

    # Frozen: a planner can't re-queue it.
    _seed(conn, [planning.event_item("basketball", int(key), 14251, 100046, 10, True)])
    assert _status(conn, planning.EVENT, key)[0] == "parsed"


def test_dropped_game_is_marked_removed(conn):
    client = FakeClient(_acb_routes())
    _backfill(conn, client)
    game_id = _load("events_next_current")["events"][0]["id"]
    _seed(conn, [planning.event_item("basketball", game_id, 264, CURRENT, 10, True)])
    _work(conn, client)  # /event/{id} is not routed: 404
    assert _q(
        conn,
        "SELECT removed_at IS NOT NULL FROM sofascore_events WHERE event_id = %s",
        game_id,
    ) == [(True,)]
    assert _status(conn, planning.EVENT, str(game_id))[0] == "none_available"


def test_finished_old_season_is_frozen_and_not_re_queued(conn):
    client = FakeClient(_acb_routes())
    _backfill(conn, client)
    _daily(conn, client)
    assert _q(conn, "SELECT season_id FROM sofascore_seasons WHERE is_complete") == [(OLD,)]
    client.calls.clear()
    _daily(conn, client)
    assert not any(f"/season/{OLD}/" in path for path, _ in client.calls)


def test_other_sport_rows_are_skipped(conn):
    routes = _acb_routes()
    other = routes["/unique-tournament/264/season/100873/events/next/0"]["events"][0]
    other["tournament"]["category"]["sport"]["slug"] = "football"
    client = FakeClient(routes)
    _backfill(conn, client)
    assert _q(conn, "SELECT COUNT(*) FROM sofascore_events WHERE event_id = %s", other["id"]) == [(0,)]
    assert _q(conn, "SELECT COUNT(*) FROM sofascore_events WHERE season_id = %s", CURRENT) == [(4,)]


def test_reparse_rebuilds_the_typed_tables(conn):
    client = FakeClient(_acb_routes())
    _backfill(conn, client)
    before = _q(
        conn,
        "SELECT event_id, home_score, status_type FROM sofascore_events ORDER BY 1",
    )
    _q(conn, "DELETE FROM sofascore_events RETURNING 1")
    _q(conn, "DELETE FROM sofascore_seasons RETURNING 1")
    client.calls.clear()
    assert run.reparse(conn, SPORTS) > 0
    assert not client.calls
    assert (
        _q(
            conn,
            "SELECT event_id, home_score, status_type FROM sofascore_events ORDER BY 1",
        )
        == before
    )
    assert _q(conn, "SELECT COUNT(*) FROM sofascore_seasons") == [(2,)]


class OneBlock(FakeClient):
    """Refuses the first ``refusals`` requests, then serves the routes."""

    def __init__(self, routes, refusals):
        super().__init__(routes)
        self.refusals = refusals

    def get(self, path, etag=None):
        if self.refusals:
            self.refusals -= 1
            self.calls.append((path, etag))
            self.request_count += 1
            raise SofascoreBlockedError("HTTP 403")
        return super().get(path, etag)


def _policy(tmp_path, max_cooldowns, sleeps):
    return run.BlockPolicy(
        cooldown_minutes=60, max_cooldowns=max_cooldowns, marker=tmp_path / "blocked", sleep=sleeps.append
    )


def test_a_cron_run_stops_at_a_block_keeps_the_item_and_marks_the_cooldown(conn, tmp_path):
    client = FakeClient({"/unique-tournament/264": 403})
    _seed(conn, run.plan_backfill([ACB]))
    policy = _policy(tmp_path, 0, [])
    with pytest.raises(SofascoreBlockedError):
        run.work(conn, client, SPORTS, max_minutes=1, block_policy=policy)
    status = _q(conn, "SELECT status, attempts FROM sofascore_queue WHERE entity_type = 'tournament'")
    assert status == [("pending", 0)]  # not counted as an attempt
    until = policy.blocked_until()
    assert until is not None and until > datetime.now(UTC) + timedelta(minutes=59)


def test_a_backfill_waits_out_one_block_and_carries_on(conn, tmp_path):
    client = OneBlock(_acb_routes(), refusals=1)
    _seed(conn, run.plan_backfill([ACB]))
    sleeps: list = []
    stats = run.work(conn, client, SPORTS, max_minutes=1, block_policy=_policy(tmp_path, 3, sleeps))
    assert sleeps == [3600] and stats.counts["blocked"] == 1
    assert _q(conn, "SELECT COUNT(*) FROM sofascore_queue WHERE status = 'pending'") == [(0,)]
    assert _q(conn, "SELECT COUNT(*) FROM sofascore_events") == [(8,)]


def test_a_refusal_right_after_a_cooldown_stops_the_backfill(conn, tmp_path):
    client = OneBlock(_acb_routes(), refusals=2)
    _seed(conn, run.plan_backfill([ACB]))
    sleeps: list = []
    with pytest.raises(SofascoreBlockedError):
        run.work(conn, client, SPORTS, max_minutes=1, block_policy=_policy(tmp_path, 3, sleeps))
    assert sleeps == [3600] and len(client.calls) == 2


def test_an_expired_marker_does_not_block(tmp_path):
    marker = tmp_path / "blocked"
    marker.write_text((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), encoding="utf-8")
    assert run.BlockPolicy(cooldown_minutes=60, marker=marker).blocked_until() is None


def test_errors_are_retried_then_failed_with_the_reason(conn):
    client = FakeClient({"/unique-tournament/264": {"no": "uniqueTournament key"}})
    _seed(conn, [planning.tournament_item("basketball", 264, 100)])
    for _ in range(run.MAX_ATTEMPTS):
        _work(conn, client)
        _q(conn, "UPDATE sofascore_queue SET refresh_after = NULL RETURNING 1")
    status, attempts, error = _q(conn, "SELECT status, attempts, last_error FROM sofascore_queue")[0]
    assert (status, attempts) == ("failed", run.MAX_ATTEMPTS) and "KeyError" in error


def test_a_run_of_failures_stops_the_run(conn, monkeypatch):
    monkeypatch.setattr(run, "MAX_CONSECUTIVE_ERRORS", 2)
    bad = {"no": "uniqueTournament key"}
    client = FakeClient({f"/unique-tournament/{tid}": bad for tid in (1, 2, 3)})
    _seed(conn, [planning.tournament_item("basketball", tid, 100) for tid in (1, 2, 3)])
    with pytest.raises(run.CollectorFailingError, match="2 items failed in a row"):
        _work(conn, client)
    assert len(client.calls) == 2


def _serie_b_routes(game_started_hours_ago=None):
    """Serie B with one finished game (the ``event`` fixture) and its stats, lineups, standings."""
    event = _load("event")
    if game_started_hours_ago is not None:
        start = datetime.now(UTC) - timedelta(hours=game_started_hours_ago)
        event["event"]["startTimestamp"] = int(start.timestamp())
    sb, season = "/unique-tournament/14251", "/unique-tournament/14251/season/100046"
    return event, {
        sb: {"uniqueTournament": {"id": 14251, "name": "Serie B"}},
        f"{sb}/seasons": {"seasons": [{"id": 100046, "name": "Serie B 26/27", "year": "26/27"}]},
        f"{season}/events/last/0": {"events": [event["event"]], "hasNextPage": False},
        f"{season}/standings/total": _load("standings_total"),
        f"{season}/rounds": _load("rounds"),
        "/event/16732145/statistics": _load("statistics"),
        "/event/16732145/lineups": _load("lineups"),
    }


def test_backfill_collects_stats_lineups_players_standings_and_rounds(conn):
    _, routes = _serie_b_routes()
    _backfill(conn, FakeClient(routes), (SERIE_B,))

    assert _q(conn, "SELECT COUNT(*), COUNT(DISTINCT side) FROM sofascore_event_team_stats") == [(12, 2)]
    assert _q(conn, "SELECT COUNT(*) FROM sofascore_players") == [(4,)]
    assert _q(
        conn,
        "SELECT points, assists, statistics->>'fieldGoalPct' FROM sofascore_event_player_stats "
        "WHERE player_id = 1645510",
    ) == [(8, 3, "33")]
    assert _q(conn, "SELECT COUNT(*), COUNT(DISTINCT block_id) FROM sofascore_standings") == [(4, 2)]
    assert _q(conn, "SELECT current_round, jsonb_array_length(rounds) FROM sofascore_seasons") == [(8, 4)]
    # Home/away tables are not routed: recorded as none_available, never retried.
    assert _status(conn, planning.STANDINGS, "14251|100046|home")[0] == "none_available"
    coverage = _q(
        conn,
        "SELECT games, with_team_stats, flagged_player_stats, with_player_stats, standings_blocks "
        "FROM v_sofascore_coverage",
    )
    assert coverage == [(1, 1, 1, 1, 2)]


def test_a_correction_re_check_re_fetches_stats_only_when_the_game_changed(conn):
    event, routes = _serie_b_routes(game_started_hours_ago=3)
    client = FakeClient(routes)
    _backfill(conn, client, (SERIE_B,))
    key = str(event["event"]["id"])
    routes[f"/event/{key}"] = copy.deepcopy(event)
    _seed(conn, [planning.event_item("basketball", int(key), 14251, 100046, 10, True)])
    _work(conn, client)
    assert _status(conn, planning.EVENT, key)[:2] == ("pending", 1)

    # Re-check 1: unchanged (304) -> the stats are not asked again.
    client.unchanged = {f"/event/{key}"}
    client.calls.clear()
    _q(conn, "UPDATE sofascore_queue SET refresh_after = now() WHERE entity_key = %s RETURNING 1", key)
    _work(conn, client)
    assert [p for p, _ in client.calls] == [f"/event/{key}"]

    # Re-check 2: Sofascore corrected the score -> statistics and lineups are fetched again.
    client.unchanged = set()
    routes[f"/event/{key}"]["event"]["homeScore"]["current"] = 86
    client.calls.clear()
    _q(conn, "UPDATE sofascore_queue SET refresh_after = now() WHERE entity_key = %s RETURNING 1", key)
    _work(conn, client)
    paths = [p for p, _ in client.calls]
    assert f"/event/{key}/statistics" in paths and f"/event/{key}/lineups" in paths
    assert _q(conn, "SELECT home_score FROM sofascore_events") == [(86,)]


def test_a_new_standings_table_replaces_the_old_one(conn):
    _, routes = _serie_b_routes()
    client = FakeClient(routes)
    _backfill(conn, client, (SERIE_B,))
    standings = routes["/unique-tournament/14251/season/100046/standings/total"]
    standings["standings"][1]["rows"].pop()  # a team leaves group B
    _daily(conn, client, (SERIE_B,))
    assert _q(conn, "SELECT COUNT(*) FROM sofascore_standings") == [(3,)]
