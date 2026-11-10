# sofascore

Basketball and hockey data from Sofascore's JSON API for the leagues AsianOdds prices but
Flashscore and Basketball API don't cover: tournaments, seasons, teams, players, games (results and
the full published schedule), team and player match stats, and standings. Stored in the
`basketball_stats` Postgres DB in `sofascore_*` tables; `realgm_db` maps them onto canonical later.

The tournaments collected are in [`sofascore/leagues.py`](sofascore/leagues.py) (58 today; the list
and how it was chosen: BAS-227).

## How it works

```
planners (backfill / daily / fast) ──▶ sofascore_queue ──▶ worker ──▶ Sofascore /api/v1
                                            ▲                │
                                            └── discovery ◀──┤ payload ──▶ sofascore_raw
                                                             └──▶ loader ──▶ sofascore_* tables
```

* Every API path is a queue row with a status; every payload is kept raw; the tables are rebuilt
  from raw, so a parser fix is a `reparse`, not a re-fetch.
* One request at a time, 4–6 s apart, with ETags (an unchanged page is a 0-byte 304). A 403/429
  (Sofascore's rate limit) means an hour without any request.
* A finished game is fetched once, re-checked after 6 h, 36 h and 7 days, then frozen.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the tables, entity types and runs.

## Setup

```bash
cd ~/dev/sofascore
poetry env use python3.13
poetry install --with=dev
cp .default.env .development.env   # then add CONNECTIONSTRING_BB_STATS and SENTRY_DSN
pre-commit install
```

direnv loads `.default.env` and `.development.env` (`.envrc`); cron does not, so the wrappers in
`local_cronjobs/` source both files.

## Commands

```bash
poetry run python -m sofascore.runners.apply_migration migrations/001_sofascore_core.sql --dry-run
poetry run python -m sofascore.run backfill --dry-run   # what would be queued
poetry run python -m sofascore.run backfill             # every season of every league
poetry run python -m sofascore.run status               # queue counts and coverage
poetry run pytest                                       # needs local Postgres binaries
```

It runs on the Mac mini; a merge to `main` deploys there (see
[local_cronjobs/README.md](local_cronjobs/README.md)).
