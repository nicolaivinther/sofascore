"""Configuration loaded from environment variables (direnv: .default.env, then .development.env)."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=False)
class Config:  # pylint: disable=invalid-name
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
    # Pause between two Sofascore requests; one request at a time (AGENTS.md rule 4).
    SOFASCORE_DELAY_S: float = field(
        default_factory=lambda: float(os.environ.get("SOFASCORE_DELAY_S", "0.25"))
    )
