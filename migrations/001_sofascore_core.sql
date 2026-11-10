-- Sofascore raw layer (BAS-228, BAS-230, BAS-231) in basketball_stats.
--
-- sofascore_queue (the work list) and sofascore_raw (every payload) drive everything; the other
-- tables are rebuilt from sofascore_raw (python -m sofascore.run reparse). No foreign keys: pages
-- arrive in any order. Every table carries sport ('basketball' / 'hockey', the realgm_db registry
-- codes). Applied by hand: python -m sofascore.runners.apply_migration <file> --dry-run first.

-- 1. Work queue ---------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sofascore_queue (
    entity_type    text NOT NULL,
    entity_key     text NOT NULL,
    url_path       text NOT NULL,
    sport          text NOT NULL CHECK (sport IN ('basketball', 'hockey')),
    tid            integer,
    season_id      integer,
    priority       integer NOT NULL DEFAULT 100,
    status         text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_progress', 'collected', 'parsed', 'failed', 'none_available')),
    attempts       integer NOT NULL DEFAULT 0,
    last_error     text,
    etag           text,
    refresh_step   smallint NOT NULL DEFAULT 0,   -- correction re-checks done on a finished game
    claimed_at     timestamptz,
    collected_at   timestamptz,
    parsed_at      timestamptz,
    refresh_after  timestamptz,                   -- not claimed before this
    frozen_at      timestamptz,                   -- never queued again
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_type, entity_key)
);
CREATE INDEX IF NOT EXISTS sofascore_queue_status_priority_idx ON sofascore_queue (status, priority);
CREATE INDEX IF NOT EXISTS sofascore_queue_refresh_after_idx
    ON sofascore_queue (refresh_after) WHERE refresh_after IS NOT NULL;
CREATE INDEX IF NOT EXISTS sofascore_queue_tid_season_idx ON sofascore_queue (tid, season_id);

-- 2. Raw store ----------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sofascore_raw (
    entity_type        text NOT NULL,
    entity_key         text NOT NULL,
    sport              text NOT NULL CHECK (sport IN ('basketball', 'hockey')),
    payload            bytea NOT NULL,                 -- gzip JSON
    payload_sha256     text NOT NULL,                  -- of the uncompressed JSON
    fetched_at         timestamptz NOT NULL DEFAULT now(),
    collector_version  text,
    PRIMARY KEY (entity_type, entity_key)
);

-- 3. Tournaments and seasons --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sofascore_tournaments (
    tid                 integer PRIMARY KEY,
    sport               text NOT NULL CHECK (sport IN ('basketball', 'hockey')),
    name                text,
    slug                text,
    category_id         integer,
    category_name       text,
    category_alpha2     text,
    category_flag       text,
    gender              text,
    tier                integer,
    has_groups          boolean,
    has_rounds          boolean,
    has_playoff_series  boolean,
    start_ts            timestamptz,
    end_ts              timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sofascore_seasons (
    tid            integer NOT NULL,
    season_id      integer NOT NULL,
    sport          text NOT NULL CHECK (sport IN ('basketball', 'hockey')),
    name           text,
    year           text,
    current_round  integer,                       -- from /rounds
    rounds         jsonb,                         -- [{round, name, slug, prefix}] as given
    is_complete    boolean NOT NULL DEFAULT false, -- frozen: never fetched again
    completed_at   timestamptz,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tid, season_id)
);

-- 4. Teams and players --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sofascore_teams (
    team_id         integer PRIMARY KEY,
    sport           text NOT NULL CHECK (sport IN ('basketball', 'hockey')),
    name            text,
    short_name      text,
    full_name       text,
    slug            text,
    name_code       text,
    gender          text,
    country_alpha2  text,
    country_name    text,
    national        boolean,
    team_type       integer,
    disabled        boolean,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sofascore_players (
    player_id       integer PRIMARY KEY,
    sport           text NOT NULL CHECK (sport IN ('basketball', 'hockey')),
    name            text,
    first_name      text,
    last_name       text,
    short_name      text,
    slug            text,
    position        text,                         -- as given (G, F, C, FG, ...)
    jersey_number   text,
    height_cm       integer,
    gender          text,
    date_of_birth   date,
    country_alpha2  text,
    country_name    text,
    updated_at      timestamptz NOT NULL DEFAULT now()  -- from the latest lineup that listed them
);

-- 5. Games --------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sofascore_events (
    event_id                     bigint PRIMARY KEY,
    sport                        text NOT NULL CHECK (sport IN ('basketball', 'hockey')),
    tid                          integer NOT NULL,
    season_id                    integer,
    tournament_id                integer,         -- the group/stage tournament
    tournament_name              text,
    group_name                   text,
    round                        integer,
    round_name                   text,
    start_ts                     timestamptz NOT NULL,
    previous_start_ts            timestamptz,     -- set when Sofascore re-dates the game
    rescheduled_at               timestamptz,
    home_team_id                 integer,
    home_team_name               text,
    away_team_id                 integer,
    away_team_name               text,
    home_score                   integer,         -- final, overtime included
    away_score                   integer,
    home_p1 integer, home_p2 integer, home_p3 integer, home_p4 integer,
    away_p1 integer, away_p2 integer, away_p3 integer, away_p4 integer,
    home_normaltime              integer,
    away_normaltime              integer,
    home_overtime                integer,         -- one aggregate over all overtimes
    away_overtime                integer,
    home_penalties               integer,         -- hockey shootout tally
    away_penalties               integer,
    status_code                  integer,
    status_type                  text,            -- notstarted, inprogress, finished, postponed, canceled
    status_description           text,
    winner_code                  integer,         -- 1 home, 2 away, 3 draw
    has_event_player_statistics  boolean,
    first_seen_at                timestamptz NOT NULL DEFAULT now(),
    last_seen_at                 timestamptz NOT NULL DEFAULT now(),
    removed_at                   timestamptz,     -- /event/{id} 404s: Sofascore dropped it
    updated_at                   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sofascore_events_tid_season_idx ON sofascore_events (tid, season_id);
CREATE INDEX IF NOT EXISTS sofascore_events_start_idx ON sofascore_events (start_ts);
CREATE INDEX IF NOT EXISTS sofascore_events_open_idx ON sofascore_events (start_ts)
    WHERE status_type IN ('notstarted', 'inprogress');

-- 6. Match stats --------------------------------------------------------------------------------
-- One row per game, period (ALL, 1Q.., as given), side and statistic. value/total are the numbers
-- (19 of 25 free throws: value 19, total 25); display is Sofascore's own text ("19/25 (76%)").
CREATE TABLE IF NOT EXISTS sofascore_event_team_stats (
    event_id     bigint NOT NULL,
    sport        text NOT NULL CHECK (sport IN ('basketball', 'hockey')),
    period       text NOT NULL,
    side         text NOT NULL CHECK (side IN ('home', 'away')),
    stat_key     text NOT NULL,
    group_name   text,
    stat_name    text,
    value        numeric,
    total        numeric,
    display      text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, period, side, stat_key)
);

-- Player box score per game. The common basketball fields are columns; statistics holds every
-- field Sofascore gives (hockey's too), so nothing is dropped.
CREATE TABLE IF NOT EXISTS sofascore_event_player_stats (
    event_id             bigint NOT NULL,
    player_id            integer NOT NULL,
    sport                text NOT NULL CHECK (sport IN ('basketball', 'hockey')),
    side                 text NOT NULL CHECK (side IN ('home', 'away')),
    team_id              integer,                 -- as given on the lineup row
    shirt_number         integer,
    jersey_number        text,
    position             text,
    substitute           boolean,
    seconds_played       integer,
    points               integer,
    rebounds             integer,
    offensive_rebounds   integer,
    defensive_rebounds   integer,
    assists              integer,
    steals               integer,
    blocks               integer,
    turnovers            integer,
    personal_fouls       integer,
    plus_minus           integer,
    field_goals_made     integer,
    field_goal_attempts  integer,
    two_points_made      integer,
    two_point_attempts   integer,
    three_points_made    integer,
    three_point_attempts integer,
    free_throws_made     integer,
    free_throw_attempts  integer,
    statistics           jsonb,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, player_id)
);
CREATE INDEX IF NOT EXISTS sofascore_event_player_stats_player_idx
    ON sofascore_event_player_stats (player_id);

-- 7. Standings ----------------------------------------------------------------------------------
-- One row per season, table type (total/home/away), block (a group or stage table) and team, as
-- Sofascore publishes it; promotion is the zone label (Playoffs, Relegation, ...).
CREATE TABLE IF NOT EXISTS sofascore_standings (
    tid                   integer NOT NULL,
    season_id             integer NOT NULL,
    standings_type        text NOT NULL CHECK (standings_type IN ('total', 'home', 'away')),
    block_id              integer NOT NULL,
    team_id               integer NOT NULL,
    sport                 text NOT NULL CHECK (sport IN ('basketball', 'hockey')),
    block_name            text,
    tournament_id         integer,
    group_name            text,
    is_group              boolean,
    position              integer,
    matches               integer,
    wins                  integer,
    draws                 integer,
    losses                integer,
    scores_for            integer,
    scores_against        integer,
    points                numeric,
    percentage            numeric,
    games_behind          numeric,
    streak                integer,
    promotion_id          integer,
    promotion_text        text,
    block_updated_at      timestamptz,             -- Sofascore's updatedAtTimestamp
    updated_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tid, season_id, standings_type, block_id, team_id)
);

-- 8. Coverage per tournament and season ----------------------------------------------------------
CREATE OR REPLACE VIEW v_sofascore_coverage AS
WITH games AS (
    SELECT tid, season_id,
           COUNT(*) AS games,
           COUNT(*) FILTER (WHERE status_type = 'finished') AS finished,
           COUNT(*) FILTER (WHERE status_type IN ('notstarted', 'inprogress')) AS open_games,
           COUNT(*) FILTER (WHERE status_type IN ('postponed', 'canceled')
                            OR removed_at IS NOT NULL) AS postponed_or_removed,
           COUNT(*) FILTER (WHERE has_event_player_statistics) AS flagged_player_stats,
           MIN(start_ts) AS first_game, MAX(start_ts) AS last_game
    FROM sofascore_events
    GROUP BY tid, season_id
), team_stats AS (
    SELECT e.tid, e.season_id, COUNT(DISTINCT t.event_id) AS with_team_stats
    FROM sofascore_event_team_stats t JOIN sofascore_events e USING (event_id)
    GROUP BY e.tid, e.season_id
), player_stats AS (
    SELECT e.tid, e.season_id, COUNT(DISTINCT p.event_id) AS with_player_stats
    FROM sofascore_event_player_stats p JOIN sofascore_events e USING (event_id)
    GROUP BY e.tid, e.season_id
), standings AS (
    SELECT tid, season_id, COUNT(DISTINCT block_id) AS standings_blocks
    FROM sofascore_standings WHERE standings_type = 'total'
    GROUP BY tid, season_id
), queue AS (
    SELECT tid, season_id,
           COUNT(*) FILTER (WHERE status IN ('pending', 'in_progress')) AS queue_pending,
           COUNT(*) FILTER (WHERE status = 'failed') AS queue_failed,
           COUNT(*) FILTER (WHERE status = 'none_available') AS queue_none_available
    FROM sofascore_queue
    WHERE season_id IS NOT NULL
    GROUP BY tid, season_id
)
SELECT s.sport, s.tid, t.name AS tournament, s.season_id, s.name AS season, s.is_complete,
       COALESCE(g.games, 0) AS games, COALESCE(g.finished, 0) AS finished,
       COALESCE(g.open_games, 0) AS open_games,
       COALESCE(g.postponed_or_removed, 0) AS postponed_or_removed,
       COALESCE(ts.with_team_stats, 0) AS with_team_stats,
       COALESCE(g.flagged_player_stats, 0) AS flagged_player_stats,
       COALESCE(ps.with_player_stats, 0) AS with_player_stats,
       COALESCE(st.standings_blocks, 0) AS standings_blocks,
       g.first_game, g.last_game,
       COALESCE(q.queue_pending, 0) AS queue_pending,
       COALESCE(q.queue_failed, 0) AS queue_failed,
       COALESCE(q.queue_none_available, 0) AS queue_none_available
FROM sofascore_seasons s
LEFT JOIN sofascore_tournaments t ON t.tid = s.tid
LEFT JOIN games g ON g.tid = s.tid AND g.season_id = s.season_id
LEFT JOIN team_stats ts ON ts.tid = s.tid AND ts.season_id = s.season_id
LEFT JOIN player_stats ps ON ps.tid = s.tid AND ps.season_id = s.season_id
LEFT JOIN standings st ON st.tid = s.tid AND st.season_id = s.season_id
LEFT JOIN queue q ON q.tid = s.tid AND q.season_id = s.season_id;
