"""Shared logger for the whole app.

Usage in any other module:

    from app.log import logger
    logger.info("something happened")
"""

from __future__ import annotations

import logging
import os
import sys

from app.config import get_settings

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOGGER_NAME = "deerstalker"


def _build_logger() -> logging.Logger:
    settings = get_settings()

    log = logging.getLogger(_LOGGER_NAME)
    log.setLevel(logging.DEBUG)
    log.propagate = False

    if log.handlers:
        # Already configured (e.g. module reloaded) — don't double-attach
        # handlers, which would duplicate every log line.
        return log

    formatter = logging.Formatter(_LOG_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)

    log_path = os.path.join(settings.app_data_dir, "deerstalker.log")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

    return log


logger = _build_logger()
