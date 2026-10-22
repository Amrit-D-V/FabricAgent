import json
import logging
import os
import sys
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = _ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)
LOG_FILE = str(LOGS_DIR / "app.log")

_EXTRA_FIELDS = (
    "tokens_in",
    "tokens_out",
    "latency_ms",
    "model",
    "agent",
    "project_id",
)


class JSONFormatter(logging.Formatter):

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_data["exception"] = traceback.format_exc()
        for field in _EXTRA_FIELDS:
            val = getattr(record, field, None)
            if val is not None:
                log_data[field] = val
        return json.dumps(log_data)


class _DropClientDisconnectNoise(logging.Filter):

    _NOISE = ("WebSocketClosedError", "StreamClosedError")

    def filter(self, record):
        if record.exc_info and type(record.exc_info[1]).__name__ in self._NOISE:
            return False
        msg = record.getMessage()
        return not any(n in msg for n in self._NOISE)


for _noisy in ("asyncio", "tornado.websocket", "tornado.application"):
    logging.getLogger(_noisy).addFilter(_DropClientDisconnectNoise())


def _console_formatter() -> logging.Formatter:
    plain = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    try:
        from config import CONSOLE_DASHBOARD

        if not CONSOLE_DASHBOARD:
            return plain
        from util.console_ui import DashboardFormatter, enable_utf8_stdout

        enable_utf8_stdout()
        return DashboardFormatter()
    except Exception:
        return plain


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JSONFormatter())
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(_console_formatter())
    logger.addHandler(console_handler)

    return logger
