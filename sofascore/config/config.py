"""Configuration loaded from environment variables (direnv: .default.env, then .development.env)."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=False)
class Config:  # pylint: disable=invalid-name,too-many-instance-attributes
    """Application configuration (names mirror the env vars)."""

    LOGLEVEL: str = field(default_factory=lambda: os.environ.get("LOGLEVEL", "INFO"))
    SENTRY_DSN: str = field(default_factory=lambda: os.environ.get("SENTRY_DSN", ""))
    SENTRY_ENVIRONMENT: str = field(
        default_factory=lambda: os.environ.get("SENTRY_ENVIRONMENT", "development")
    )
    # The basketball_stats DB, shared with realgm_db, flashscore_stats and eliteprospects.
    CONNECTIONSTRING_BB_STATS: str = field(
        default_factory=lambda: os.environ.get("CONNECTIONSTRING_BB_STATS", "")
    )
    # Random pause between two Sofascore requests, seconds; one request at a time (AGENTS.md rule 4).
    SOFASCORE_PAUSE_MIN_S: float = field(
        default_factory=lambda: float(os.environ.get("SOFASCORE_PAUSE_MIN_S", "2"))
    )
    SOFASCORE_PAUSE_MAX_S: float = field(
        default_factory=lambda: float(os.environ.get("SOFASCORE_PAUSE_MAX_S", "4"))
    )
    # After a 403/429 nothing asks Sofascore for this many minutes (any run, cron or by hand).
    SOFASCORE_COOLDOWN_MIN: float = field(
        default_factory=lambda: float(os.environ.get("SOFASCORE_COOLDOWN_MIN", "60"))
    )
    # Where the "blocked until" marker lives; relative to the repo root the runs start in.
    SOFASCORE_STATE_DIR: str = field(default_factory=lambda: os.environ.get("SOFASCORE_STATE_DIR", "logs"))
