# Architecture

## Access: read this before debugging a 403

- `www.sofascore.com/api/v1/...` is the only host (`api.sofascore.com` 403s everything). Plain
  `requests` is fingerprint-blocked, so `transport.CurlTransport` sends through `curl_cffi` with one
  desktop Chrome profile per session, opens the home page once first, and sends the headers of a
  same-origin `fetch()` (`transport.API_HEADERS`), including the web app's `X-Requested-With`
  (first 6 hex digits of SHA-256 of the 30-minute slot number, `transport.requested_with`).
- **Captcha challenge:** the API can answer 403 `{"error": {"reason": "challenge"}}`. The web app
  then opens `/captcha.html`, which runs Cloudflare Turnstile, posts the result to
  `/api/v1/token/captcha` and gets a JWT valid for one hour. The app keeps it as the `sofa_captcha`
  cookie and sends it as `X-Captcha`. **The token works only inside the browser session that solved
  it:** curl with the same token, and a new Chrome launch (visible or headless) with it, are both
  challenged again. So it is never stored.
- `captcha.BrowserSession` passes the challenge in the installed Google Chrome through patchright (a
  Playwright that hides automation; a plain automated browser fails Turnstile with error 600010),
  with a fresh profile per launch under `logs/sofascore_browser_profiles/` (deleted on close).
- **On a challenge** the client has the browser solve (at most once per 10 minutes) and from then on
  sends every request of the run as `fetch()` from that page, still paced. The token lasts an hour;
  when it expires mid-run the same browser solves again. A challenge right after a solve, or a
  failed solve, is a refusal like any other. `python -m sofascore.run captcha` opens a visible
  Chrome window for a person to pass by hand (no auto-click) and fetches one page from it, to
  check this machine; `SOFASCORE_BROWSER_HEADLESS=0` makes the runs show their window.
- **Rate limit:** Sofascore answered 403 after about 1,500 requests at 2.7 a second, and after about
  2,150 at one per ~3.1 s; the second block still held after an hour (2026-11-10). At one per ~4.1 s
  it took 3,000 without one. So the client pauses a random 4–6 s between requests and never
  retries any other 403/429: it raises `SofascoreBlockedError`, the item goes back to pending (no
  attempt counted), and `logs/sofascore_blocked_until` stops every run from asking for 60 minutes. A backfill waits the
  hour out and carries on (at most 3 times per run); a refusal on the first request after a
  cooldown, or in a cron run, stops the run (exit code 2, Sentry).
- Twenty failed items in a row stop the run (exit code 3, Sentry) instead of burning the queue.
- If refusals persist after cooldowns, the defence may have changed: re-probe by hand, never loop.
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

Every table has `sport` (`basketball`). `sofascore_events.home_p1..p4` are the quarters.

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
  our rows and started 100 minutes to 48 hours ago. A finished game then queues its
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

About 700 requests an hour (a 4–6 s pause plus the response). Full history of the 57 leagues:
about 3,000 requests for tournaments, seasons, game pages and teams, plus about 2 per finished
game for stats and lineups (up to ~50,000), plus 4 per season for standings and rounds: roughly
3 days, once, resumable. A daily pass is a few hundred mostly-304 requests (25–50 minutes); a
fast run a few to ~30.

## Status queries

```sql
SELECT entity_type, status, COUNT(*) FROM sofascore_queue GROUP BY 1, 2 ORDER BY 1, 2;
SELECT entity_type, entity_key, last_error FROM sofascore_queue WHERE status = 'failed';
SELECT * FROM v_sofascore_coverage WHERE tid = 14251 ORDER BY season_id;
```

## Not collected

Play-by-play (`/event/{id}/incidents`, about 54 KB per game; no consumer yet). `/player/{id}`:
lineups carry the player fields we store.
