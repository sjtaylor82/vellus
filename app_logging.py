from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from config import APP_DIR, LOG_PATH


def setup_logging() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(handler, "baseFilename", None) == str(LOG_PATH) for handler in root.handlers):
        return
    handler = RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
