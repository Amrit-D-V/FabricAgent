from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from api.registry import ROUTES, error_response
from auth.session_cookie import COOKIE_NAME, parse_session
from core import context

# Importing a route module is what registers its routes.
from api import (  # noqa: F401
    routes_admin,
    routes_auth,
    routes_builder,
    routes_chat,
    routes_documents,
    routes_jobs,
    routes_models,
    routes_projects,
    routes_reports,
    routes_storage,
)

logger = logging.getLogger("fabricbi.api")

FRONTEND_PORT = os.getenv("VITE_PORT", "5173")
BIND_HOST = os.getenv("FABRICBI_AGENT_HOST", "127.0.0.1").strip()
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
IS_SHARED = BIND_HOST not in LOOPBACK_HOSTS


def _frontend_origins() -> list[str]:
    """Browser origins allowed to call this API.

    Credentials are sent (the session cookie), so this can never be "*".
    Override with FABRICBI_FRONTEND_ORIGINS when the UI is reached by a name this
    cannot guess.
    """
    override = os.getenv("FABRICBI_FRONTEND_ORIGINS", "").strip()
    if override:
        return [o.strip().rstrip("/") for o in override.split(",") if o.strip()]

    origins = [
        f"http://localhost:{FRONTEND_PORT}",
        f"http://127.0.0.1:{FRONTEND_PORT}",
    ]
    if IS_SHARED and BIND_HOST != "0.0.0.0":
        origins.append(f"http://{BIND_HOST}:{FRONTEND_PORT}")
    return origins


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


class RequestContext(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        from starlette.concurrency import run_in_threadpool

        from auth.auth import user_from_session

        payload = parse_session(request.cookies.get(COOKIE_NAME, ""))
        try:
            user = (
                await run_in_threadpool(user_from_session, payload) if payload else None
            )
        except Exception as exc:  # noqa: BLE001 - any failure here is "not signed in"
            return error_response(exc)

        token = context.begin(user, ip=_client_ip(request))
        try:
            _adopt_active_project(request)
            try:
                return await call_next(request)
            except Exception as exc:
                return error_response(exc)
        finally:
            context.end(token)


def _adopt_active_project(request: Request) -> None:
    """The project the UI is scoped to, sent as a header on every call.

    It also goes into audit_context, which is where the audit log and
    llm_client's per-project model override both read it from.
    """
    raw = (request.headers.get("X-FabricBI-Project") or "").strip()
    if not raw.isdigit():
        return
    project_id = int(raw)
    context.state()["active_project_id"] = project_id
    try:
        from db import audit_context

        audit_context.set_project(project_id=project_id)
    except Exception:
        pass


async def _not_found(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"error": "No such endpoint."}, status_code=404)


app = Starlette(
    routes=ROUTES,
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=_frontend_origins(),
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "content-type",
                "authorization",
                "x-fabricbi-project",
            ],
            allow_credentials=True,
        ),
        Middleware(RequestContext),
    ],
    exception_handlers={404: _not_found},
)


def _require_auth_when_shared() -> None:
    """Refuse to bind a network interface that would leak the session cookie.

    This used to demand FABRICBI_AGENT_TOKEN, on the reasoning that the tools can
    modify a live semantic model so the port must not be open unauthenticated.
    The reasoning was right and the check was theatre: nothing ever validated
    that token. Every route is gated by the session cookie instead -- verified
    by sending the real token with no cookie and getting 401 -- so the token
    added a requirement, not a defence.

    What actually goes wrong on a shared bind is narrower and worse. The
    session cookie is the whole credential, and without Secure it crosses the
    network in cleartext, where anyone on that network can lift it and replay
    it. Entra never enters into it: a stolen cookie is already signed in.

    So the condition is the one that matters -- do not offer this to a network
    unless the cookie is protected in transit.
    """
    from config import SESSION_COOKIE_SECURE

    if not IS_SHARED or SESSION_COOKIE_SECURE:
        return

    if os.getenv("FABRICBI_ALLOW_INSECURE_SHARED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        print(
            f"WARNING: bound to {BIND_HOST} over plain HTTP. The session cookie "
            "is not Secure, so anyone on this network can capture and replay it. "
            "Acceptable only on a network you trust completely."
        )
        return

    raise SystemExit(
        f"Refusing to bind {BIND_HOST} while the session cookie is not Secure.\n"
        "\n"
        "The cookie is the entire credential -- a captured one is a signed-in\n"
        "session, with no Microsoft prompt to get past. Over plain HTTP on a\n"
        "shared interface it is readable by anyone on the network.\n"
        "\n"
        "Pick one:\n"
        "  - keep it private:  FABRICBI_AGENT_HOST=127.0.0.1  (the default)\n"
        "  - serve over TLS:   set ENTRA_REDIRECT_URI to an https:// URL, which\n"
        "                      turns SESSION_COOKIE_SECURE on\n"
        "  - accept the risk:  FABRICBI_ALLOW_INSECURE_SHARED=1 (trusted LAN only)"
    )


def preflight_check() -> None:
    from config import FABRICBI_TEMP_DIR, PROJECTS_DIR
    from db.database import UPLOADS_DIR
    from core.jobs import init_jobs_table

    for path in (PROJECTS_DIR, FABRICBI_TEMP_DIR, UPLOADS_DIR):
        os.makedirs(path, exist_ok=True)
        test_file = os.path.join(path, ".fabricbi_write_test")
        try:
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(test_file)
        except OSError as e:
            logger.warning(f"Storage preflight write warning on {path}: {e}")

    try:
        init_jobs_table()
    except Exception as e:
        logger.warning(f"Background jobs preflight check warning: {e}")



if __name__ == "__main__":
    import uvicorn

    from db.database import init_db

    _require_auth_when_shared()
    init_db()
    preflight_check()


    port = int(
        os.getenv(
            "FABRICBI_API_PORT",
            os.getenv("FABRICBI_AGENT_PORT", "8756"),
        )
    )
    reach = "localhost" if BIND_HOST in LOOPBACK_HOSTS else BIND_HOST
    dev = os.getenv("FABRICBI_DEV", "").strip().lower() in ("1", "true", "yes")
    print(f"FabricBI API on http://{reach}:{port}")
    print(f"  CORS origins: {', '.join(_frontend_origins())}")
    print(f"  routes: {len(ROUTES)}")
    print(f"  auto-reload: {'on' if dev else 'off (set FABRICBI_DEV=1)'}")
    if dev:
        uvicorn.run(
            "api.server:app", host=BIND_HOST, port=port, log_level="info", reload=True
        )
    else:
        uvicorn.run(app, host=BIND_HOST, port=port, log_level="info")
