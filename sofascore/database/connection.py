"""psycopg2 connections to the basketball_stats DB."""

import psycopg2
from psycopg2.extensions import connection

from sofascore.config.config import Config


def connect(config: Config | None = None) -> connection:
    """Open a new connection. Raises RuntimeError when the connection string is not configured."""
    config = config or Config()
    if not config.CONNECTIONSTRING_BB_STATS:
        raise RuntimeError("CONNECTIONSTRING_BB_STATS is not set (put it in .development.env)")
    return psycopg2.connect(config.CONNECTIONSTRING_BB_STATS)
