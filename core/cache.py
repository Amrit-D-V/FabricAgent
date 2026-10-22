from __future__ import annotations

import functools
import time
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def _hashable(obj: Any) -> Any:
    if isinstance(obj, (list, tuple)):
        return tuple(_hashable(x) for x in obj)
    if isinstance(obj, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in obj.items()))
    if isinstance(obj, set):
        return tuple(sorted(_hashable(x) for x in obj))
    return obj


def ttl_cache(seconds: float) -> Callable[[F], F]:
    def decorate(fn: F) -> F:
        store: dict[tuple, tuple[float, Any]] = {}

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                key = (args, tuple(sorted(kwargs.items())))
                hash(key)
            except TypeError:
                key = (_hashable(args), _hashable(kwargs))

            hit = store.get(key)
            now = time.monotonic()
            if hit and now - hit[0] < seconds:
                return hit[1]

            value = fn(*args, **kwargs)
            store[key] = (now, value)
            return value

        wrapper.cache_clear = store.clear
        return wrapper

    return decorate
