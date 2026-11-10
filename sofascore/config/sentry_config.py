"""Sentry error monitoring: logger.error() and above are reported once setup_sentry() has run."""

import logging
import os
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration


def setup_sentry() -> bool:
    """Initialise Sentry. Returns False when SENTRY_DSN is not set."""
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        logging.getLogger("sofascore").warning("SENTRY_DSN not set. Error monitoring disabled.")
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "development"),
        integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        traces_sample_rate=0.0,
    )
    return True


def sentry_report_error(error: Exception, extra: dict[str, Any] | None = None) -> None:
    """Report a caught exception to Sentry with optional context."""
    with sentry_sdk.new_scope() as scope:
        for key, value in (extra or {}).items():
            scope.set_extra(key, value)
        sentry_sdk.capture_exception(error)
