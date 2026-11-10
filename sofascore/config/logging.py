"""Logging configuration."""

import logging
from logging import Formatter, StreamHandler, getLogger


def setup_logger(name: str = "sofascore", level: str = "INFO") -> logging.Logger:
    """Set up and return a configured logger."""
    log = getLogger(name)
    if log.hasHandlers():
        log.handlers.clear()
    handler = StreamHandler()
    handler.setFormatter(Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    log.addHandler(handler)
    log.propagate = False
    return log
