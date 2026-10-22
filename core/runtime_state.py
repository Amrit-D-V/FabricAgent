from __future__ import annotations

import threading
from typing import Any

_lock = threading.RLock()
token: str | None = None
credential: Any = None
last_good_model_meta: dict | None = None


def get_cached_metadata() -> dict | None:
    with _lock:
        return last_good_model_meta


def set_cached_metadata(meta: dict | None) -> None:
    global last_good_model_meta
    with _lock:
        last_good_model_meta = meta


def reset() -> None:
    global token, credential, last_good_model_meta
    with _lock:
        token = None
        credential = None
        last_good_model_meta = None

