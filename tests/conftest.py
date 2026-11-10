import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
import pytest

from sofascore.database.migrations import migration_files

TABLES = (
    "sofascore_queue",
    "sofascore_raw",
    "sofascore_tournaments",
    "sofascore_seasons",
    "sofascore_teams",
    "sofascore_players",
    "sofascore_events",
    "sofascore_event_team_stats",
    "sofascore_event_player_stats",
    "sofascore_standings",
)


def _pg_bin() -> Path | None:
    """Directory with initdb/pg_ctl: SOFASCORE_TEST_PG_BIN, PATH, or Homebrew's postgresql@16."""
    candidates = [os.environ.get("SOFASCORE_TEST_PG_BIN"), str(Path(shutil.which("initdb") or "").parent)]
    candidates += ["/opt/homebrew/opt/postgresql@16/bin", "/usr/local/opt/postgresql@16/bin"]
    for candidate in candidates:
        if candidate and (Path(candidate) / "initdb").exists():
            return Path(candidate)
    return None


@pytest.fixture(scope="session", name="pg_dsn")
def pg_dsn_fixture(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A throwaway local Postgres cluster with every migration applied. Never touches prod."""
    bin_dir = _pg_bin()
    if bin_dir is None:
        pytest.skip("no local Postgres binaries (set SOFASCORE_TEST_PG_BIN)")
    data = tmp_path_factory.mktemp("pgdata")
    sock = Path(tempfile.mkdtemp(prefix="sfpg"))  # short path: unix socket names are length-limited
    env = {**os.environ, "LC_ALL": "C"}  # the server refuses to start under an invalid locale
    subprocess.run(
        [bin_dir / "initdb", "-D", data, "-U", "test", "-A", "trust", "--no-locale", "-E", "UTF8"],
        check=True,
        capture_output=True,
        env=env,
    )
    port = "5498"
    # -l: the server inherits pg_ctl's stdout, so capturing it would block until the server exits
    subprocess.run(
        [bin_dir / "pg_ctl", "-D", data, "-l", data / "server.log", "-w", "start",
         "-o", f"-k {sock} -p {port} -c listen_addresses=''"],
        check=True,
        stdout=subprocess.DEVNULL,
        env=env,
    )  # fmt: skip
    dsn = f"host={sock} port={port} user=test dbname=postgres"
    try:
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            for path in migration_files():
                cur.execute(path.read_text(encoding="utf-8"))
        conn.close()
        yield dsn
    finally:
        subprocess.run(
            [bin_dir / "pg_ctl", "-D", data, "-m", "immediate", "stop"], check=False, capture_output=True
        )
        shutil.rmtree(sock, ignore_errors=True)


@pytest.fixture(name="conn")
def conn_fixture(pg_dsn: str) -> Iterator[Any]:
    """A connection to the test cluster; every sofascore_ table is emptied first."""
    conn = psycopg2.connect(pg_dsn)
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(TABLES)}")
    conn.commit()
    yield conn
    conn.close()
