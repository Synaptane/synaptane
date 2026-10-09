"""Synaptane logs: one format shared by the web server, the worker and the migrations.

One line per event: ISO 8601 time with offset, level, module. Level set by
`SYNAPTANE_LOG_LEVEL` (INFO by default).
"""

import logging
import logging.config
import os
import re
from typing import Any

FORMAT = "%(asctime)s %(levelname)s %(name)s : %(message)s"
_SECRET_PATH = re.compile(r"/(reset|invite|verify-email|oauth/consent)/[^/?\s]+")


class AccessFilter(logging.Filter):
    """Access log without secrets: link tokens (invitation, reset, verification) and query
    parameters masked; `/readyz` (probe) omitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            if path.startswith(("/readyz", "/healthz")):
                return False
            base, _, query = path.partition("?")
            path = _SECRET_PATH.sub(r"/\1/…", base) + ("?…" if query else "")
            record.args = (*args[:2], path, *args[3:])
        return True


DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def config() -> dict[str, Any]:
    level = os.environ.get("SYNAPTANE_LOG_LEVEL", "INFO").upper()
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"plain": {"format": FORMAT, "datefmt": DATEFMT}},
        "filters": {"access": {"()": AccessFilter}},
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "plain"},
            "access": {
                "class": "logging.StreamHandler",
                "formatter": "plain",
                "filters": ["access"],
            },
        },
        "root": {"handlers": ["console"], "level": level},
        "loggers": {
            # uvicorn: its logs go through the same format (time included).
            "uvicorn": {"handlers": ["console"], "level": level, "propagate": False},
            "uvicorn.error": {"level": level},
            "uvicorn.access": {"handlers": ["access"], "level": level, "propagate": False},
            "alembic": {"level": "INFO"},  # migrations applied, from which revision to which
            "httpx": {"level": "WARNING"},
            "mcp": {"level": "WARNING"},
        },
    }


def configure() -> dict[str, Any]:
    """Configure the process logs; return the configuration (for uvicorn)."""
    settings = config()
    logging.config.dictConfig(settings)
    return settings
