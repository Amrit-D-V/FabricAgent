"""The route decorator -- where every cross-cutting concern is handled once.

A handler is a plain function of the request that returns JSON-able data.
Everything around it (is the caller signed in, does their role clear the bar,
running blocking database and Fabric work off the event loop, turning an
:class:`AppError` into a status code, serialising the result) happens here, so
route modules stay declarative and no endpoint can forget the auth check.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Iterable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.errors import AppError, NotAuthenticated
from db import audit_context

logger = logging.getLogger("fabricbi.api")

ROUTES: list[Route] = []


def route(
    path: str,
    methods: Iterable[str] = ("GET",),
    *,
    public: bool = False,
    capability: str | None = None,
    project: bool = True,
) -> Callable:
    """Register `path`.

    public
        Reachable without a session -- sign-in endpoints and /health only.
    capability
        Minimum effective role ("editor"/"admin"), checked *after* the Fabric
        workspace cap. See auth.auth.require_capability.
    project
        Requires a project to be selected, and defaults to True so that a new
        endpoint cannot forget. Set False only for what must work *before* a
        project can exist (sign-in, the project list itself) or for genuinely
        global resources (users, audit, storage, Fabric connections).
    """

    def decorate(fn: Callable) -> Callable:
        async def handler(request: Request) -> Response:
            from core.context import current_user

            if not public and current_user() is None:
                raise NotAuthenticated("Sign in to continue.")
            if project and not public:
                from auth.auth import require_project

                await run_in_threadpool(require_project)
            if capability:
                from auth.auth import require_capability

                require_capability(capability, what=f"{request.method} {path}")

            if inspect.iscoroutinefunction(fn):
                data = await fn(request)
            else:
                snapshot = audit_context.get_context()

                def call():
                    audit_context.adopt_context(snapshot)
                    return fn(request)

                data = await run_in_threadpool(call)

            if isinstance(data, Response):
                return data
            return JSONResponse(data if data is not None else {})

        handler.__name__ = fn.__name__
        ROUTES.append(Route(path, handler, methods=list(methods)))
        return fn

    return decorate


def error_response(exc: Exception) -> JSONResponse:
    """The one place an exception becomes a status code."""
    if isinstance(exc, AppError):
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    logger.exception("unhandled error")
    return JSONResponse({"error": "Something went wrong."}, status_code=500)


async def json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise AppError("Body must be JSON.") from None
    if not isinstance(body, dict):
        raise AppError("Body must be a JSON object.")
    return body
