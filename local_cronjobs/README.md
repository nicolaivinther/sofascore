# local_cronjobs

Cron wrappers for the Mac mini. Cron does not run direnv, so each wrapper sources `.default.env`
and `.development.env` itself. They are wrapped in realgm_db's `run_job.sh`, which records every
run in `cron_job_runs` (the Cron Jobs dashboard page).

```cron
# Sofascore daily pass (04:40, 16:40) and fast run (every 30 min); the CLI's lock keeps them apart
40 4,16 * * * /Users/nicolaitanghoj/dev/realgm_db/local_cronjobs/run_job.sh sofascore-daily /Users/nicolaitanghoj/dev/sofascore/local_cronjobs/run_sofascore_daily.sh
5,35 * * * * /Users/nicolaitanghoj/dev/realgm_db/local_cronjobs/run_job.sh sofascore-fast /Users/nicolaitanghoj/dev/sofascore/local_cronjobs/run_sofascore_fast.sh
```

After a crontab edit, sync the schedule into `cron_jobs` from realgm_db:
`poetry run python -m src.cron_monitoring.crontab_sync`.

Logs: `logs/sofascore_daily.log`, `logs/sofascore_fast.log`.

## Deploy

A merge to `main` runs `.github/workflows/deploy.yml` on the repo's own self-hosted runner on the
Mac mini (label `sofascore`): `git pull` and `poetry sync`. The next cron run uses the new code.
Migrations are never applied by the deploy.
