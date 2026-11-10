# AGENTS.md

Project instructions for any coding agent (Claude Code, Antigravity `agy`, Codex, …). `CLAUDE.md`
only imports this file.

## Hard rules

1. **No credentials in git.** Secrets live only in the untracked `.development.env`; refer to them
   by variable name (`CONNECTIONSTRING_BB_STATS`, `SENTRY_DSN`). Never open, print or copy env files.
2. **Prod DB is SELECT-only by default.** Every `INSERT`/`UPDATE`/`DELETE`/DDL needs a `--dry-run`
   first, the output shown to the user, and explicit approval for that specific operation.
   Migrations are applied by hand, with approval.
3. **Never commit to `main` and never merge PRs.** One branch (and worktree) per `BAS-<n>` ticket;
   the user merges.
4. **Collection pacing is fixed:** one request at a time, `SOFASCORE_DELAY_S` (0.25 s) between
   calls, backoff on 429/5xx, and a sustained 403 stops the run and alerts (exit code 2, Sentry).
   No parallel workers, no proxies or other IPs. Only one collector runs at a time (the CLI holds
   `/tmp/sofascore_run.lock`).
5. **Write only `sofascore_*` tables.** Mapping onto canonical happens in `realgm_db`, which reads
   these tables.
6. **Outward-facing actions need confirmation:** Linear writes, GitHub actions beyond pushing your
   own branch, anything that sends mail.

## Project

* Package `sofascore/`: `client` (HTTP), `planning` (queue items and what each page leads to),
  `store` (queue + raw store), `parser` (pure JSON -> rows), `loader` (rows -> tables), `run`
  (the CLI), `leagues` (the tournaments collected), `sports`. Hand-applied SQL in `migrations/`.
* DB: `basketball_stats` (shared with `realgm_db`, `flashscore_stats`, `eliteprospects`). Every
  table is `sofascore_`-prefixed and carries `sport` (`basketball` / `hockey`, the realgm_db
  registry codes).
* Ids: keep Sofascore ids everywhere (tournament `tid`, `season_id`, `event_id`, `team_id`,
  `player_id`). Never invent surrogate keys.
* Raw first: every payload goes to `sofascore_raw`; tables are rebuilt from it (`run reparse`).
* Tickets: Linear team "Basketball Data" (`BAS`), parent BAS-226. PR title `BAS-<n>: <summary>`,
  Linear link in the body.

## Code standards

* Python 3.13, max line length 110, Black + isort + Ruff + Pylint + Mypy (all via pre-commit).
* argparse for every CLI; every mutating CLI has `--dry-run`.
* Parsers are pure functions with stored payloads as test fixtures in `tests/fixtures/`.
* New functionality ships with tests; SQL is tested against the throwaway Postgres in
  `tests/conftest.py`, never prod. pylint and mypy must be clean.
* Keep `ARCHITECTURE.md` and the READMEs current in the same PR.
