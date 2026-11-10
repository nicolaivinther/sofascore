# Architecture

## Access: read this before debugging a 403

- `www.sofascore.com/api/v1/...` answers a real browser and the `cloudscraper` Firefox/Windows
  profile (`client.DEFAULT_BROWSER`). `api.sofascore.com` 403s every profile; plain `requests` is
  fingerprint-blocked. No key, login or cookies.
- A sustained 403 stops the run (`SofascoreBlockedError`, exit code 2, Sentry): the fingerprint
  defence changed. Re-probe by hand; don't loop. Fallback if cloudscraper stops working:
  `curl_cffi` (browser TLS impersonation), not installed.
- To look at the API by hand, open any `https://www.sofascore.com/api/v1/...` URL in a browser,
  or `fetch('/api/v1/...')` from a sofascore.com page. The category list
  (`/sport/basketball/categories`) hides out-of-season countries; `/search/unique-tournaments?q=`
  finds them.
- Terms: private API, `robots.txt` 403s. Accepted as a risk on BAS-226.

## A queue and a raw store

Built like the Elite Prospects collector. Every API path we fetch is a row in `sofascore_queue`;
every payload is kept in `sofascore_raw` (gzip JSON + sha256); the typed tables are rebuilt from
raw. All tables come from `migrations/001_sofascore_core.sql`; no foreign keys, since pages
arrive in any order.

| Table | Key | What |
| -- | -- | -- |
| `sofascore_queue` | `entity_type, entity_key` | the work list: path, status, attempts, last error, ETag, re-check step, `refresh_after`, `frozen_at` |
| `sofascore_raw` | `entity_type, entity_key` | the latest payload of each item |
| `sofascore_tournaments` | `tid` | tournament detail, category as given |
| `sofascore_seasons` | `tid, season_id` | seasons, current round and rounds; `is_complete` = frozen |
| `sofascore_teams` | `team_id` | team detail, country as given |
| `sofascore_players` | `player_id` | players seen in lineups: name, position, height, birth date, country |
| `sofascore_events` | `event_id` | every game: teams, final, periods 1-4, normal time, overtime (one aggregate), shootout, status, re-dates, `removed_at` |
| `sofascore_event_team_stats` | `event_id, period, side, stat_key` | team match stats, one row per statistic (value, total, Sofascore's display text) |
| `sofascore_event_player_stats` | `event_id, player_id` | player box scores: the common basketball fields as columns, every field in `statistics` jsonb |
| `sofascore_standings` | `tid, season_id, standings_type, block_id, team_id` | total/home/away tables, every group block, promotion labels |
| `v_sofascore_coverage` | `tid, season_id` | games, finished, open, with team stats, with player stats, standings tables, queue pending/failed/none_available |

Every table has `sport` (`basketball` / `hockey`). `sofascore_events.home_p1..p4` are quarters in
basketball, periods 1-3 in hockey; `home_penalties` is the hockey shootout tally.

Queue item status: `pending → in_progress → collected → parsed`, `none_available` on a 404 (no
schedule left, no stats for a game, a cup without standings), `failed` after 5 attempts with
`last_error`.

## Entity types and discovery

Paths are in `sofascore/planning.py`. Each fetched page queues what it leads to:

- `seasons` → per season: the first `events_last` and `events_next` page, the three `standings`
  tables and `rounds`;
- an events page with `hasNextPage` → the next page; a schedule page always re-queues the next
  one, so re-fetching page 0 re-walks the whole published schedule;
- an events page or a game → every `team` seen, and every finished game's `event_statistics`
  and, when `hasEventPlayerStatistics` is set, its `event_lineups`. Team stats exist for some
  leagues without player stats (Tunisia, NBL China), so they are asked for every finished game.

Order doesn't matter: the data is linked by ids (`event_id`, `player_id`, `tid`, `season_id`,
`team_id`), and completeness is a query on the queue and the coverage view.

## Runs

```bash
poetry run python -m sofascore.run backfill   # seed every season of the leagues, then work
poetry run python -m sofascore.run daily      # what the daily cron runs
poetry run python -m sofascore.run fast       # what the 30-minute cron runs
poetry run python -m sofascore.run work       # drain the queue only
poetry run python -m sofascore.run reparse    # rebuild typed tables from raw, no HTTP
poetry run python -m sofascore.run status     # queue counts, ghosts, coverage per league
```

All take `--tid` (repeatable), `--sport`, `--dry-run` (plan and report, write and fetch nothing)
and `--max-minutes`/`--max-items`. One run at a time (`/tmp/sofascore_run.lock`); a second exits.

- **Backfill**: tournament + season list per league; discovery queues every season's pages,
  standings, rounds, games' stats and lineups, and teams.
- **Daily pass** (`sofascore-daily`, 04:40 and 16:40): freezes finished seasons, then re-queues
  every league's tournament and season list and, per open season, results page 0, schedule page
  0 (the whole schedule is re-walked: new rounds, re-dates, postponements, cancellations), the
  standings and the rounds. Unchanged pages are 304s.
- **Fast run** (`sofascore-fast`, every 30 min): `/event/{id}` for games that are not finished in
  our rows and started 100 minutes (hockey 130) to 48 hours ago. A finished game then queues its
  stats and lineups. These items jump the queue; backfill items wait for the next daily.
- **Correction re-checks**: a finished game is fetched again 6 h, 36 h and 7 days later, then
  frozen. If a re-check finds a changed payload, its stats and lineups are fetched again.
- **Frozen seasons**: not the tournament's newest season, nothing scheduled or live, newest game
  more than 14 days old, and its schedule page 404s.
- **Ghosts**: `status` lists fixtures not seen on any schedule page for 36 h; when such a game's
  start passes, the fast run asks for it, and a 404 sets `removed_at`.
- A finished game is never turned back into a fixture by a lagging list page; a game's stats and
  a season's standings table are replaced as a whole; a row whose Sofascore sport is not the
  league's sport is skipped.

## Cost

About 2-3 requests a second. Full history of the 58 leagues: about 3,000 requests for
tournaments, seasons, game pages and teams, plus about 2 per finished game for stats and lineups
(up to ~50,000, 6-7 hours, once), plus 4 per season for standings and rounds. A daily pass is a
few hundred mostly-304 requests; a fast run a few to ~30.

## Status queries

```sql
SELECT entity_type, status, COUNT(*) FROM sofascore_queue GROUP BY 1, 2 ORDER BY 1, 2;
SELECT entity_type, entity_key, last_error FROM sofascore_queue WHERE status = 'failed';
SELECT * FROM v_sofascore_coverage WHERE tid = 14251 ORDER BY season_id;
```

## Not collected

Play-by-play (`/event/{id}/incidents`, about 54 KB per game; no consumer yet). `/player/{id}`:
lineups carry the player fields we store.
