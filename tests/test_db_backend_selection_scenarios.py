"""Which store db.database picks, and why it must not guess.

`DB_BACKEND` decides whether every one of the ~45 `_get_connection()` call
sites lands on the local `project_data.db` or on the Fabric SQL Database. It is
read once, at module import, into a constant -- which makes the *timing* of
that read part of the contract:

    app.py:48   from auth.auth import require_login
    auth/auth.py:8   from db.database import audit, get_app_user, init_db
    app.py:82   load_dotenv()

`db.database` is fully imported at app.py line 48. Its `os.getenv("DB_BACKEND")`
therefore ran 34 lines before `.env` was read, took the "sqlite" default, and
the app went on reading and writing the local file while `.env` said `fabric`.
Nothing raised. The cutover looked complete for a full day.

So two things are pinned here:

  * `db/database.py` loads `.env` ITSELF, before it reads any environment
    variable. It cannot rely on the entry point, because it has four of them
    (app.py, the two MCP servers, chat_api_server.py) plus every script.
  * An unrecognised value raises instead of falling back. A silent fallback to
    local storage is exactly how the bug above stayed invisible; a typo like
    `farbic` must fail loudly rather than quietly meaning "use the local file".

The first is checked by parsing rather than importing: the module is already in
`sys.modules` by the time any test runs, so an import here would prove nothing
about the order the *first* import used.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATABASE_PY = ROOT / "db" / "database.py"


def _module_body() -> list[ast.stmt]:
    return ast.parse(DATABASE_PY.read_text(encoding="utf-8")).body


def _line_of_first_call(body: list[ast.stmt], func_name: str) -> int | None:
    """Line of the first module-level call to `func_name`, or None."""
    for node in body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Name) and fn.id == func_name:
                return node.lineno
    return None


def _line_of_assignment(body: list[ast.stmt], target: str) -> int | None:
    for node in body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == target:
                    return node.lineno
    return None


def test_the_module_loads_dotenv_itself():
    """Not left to the entry point -- there are five of them."""
    assert _line_of_first_call(_module_body(), "load_dotenv") is not None, (
        "db/database.py must call load_dotenv() at module scope. Without it, "
        "DB_BACKEND resolves against whatever env exists at first import, "
        "which for app.py is 34 lines before its own load_dotenv()."
    )


def test_dotenv_is_loaded_before_the_backend_is_read():
    """Order is the whole bug. A load_dotenv() below the read fixes nothing."""
    body = _module_body()
    loaded = _line_of_first_call(body, "load_dotenv")
    read = _line_of_assignment(body, "DB_BACKEND")
    assert loaded is not None and read is not None
    assert loaded < read, (
        f"load_dotenv() is at line {loaded} but DB_BACKEND is read at line "
        f"{read}. Reading first means .env is ignored."
    )


def test_dotenv_is_loaded_before_the_db_path_is_read():
    """DB_PATH has the same import-time exposure as DB_BACKEND."""
    body = _module_body()
    loaded = _line_of_first_call(body, "load_dotenv")
    read = _line_of_assignment(body, "DB_PATH")
    assert loaded is not None and read is not None
    assert loaded < read


def _import_with(backend: str) -> subprocess.CompletedProcess:
    """Import db.database in a fresh interpreter with DB_BACKEND set.

    A subprocess, because the constant is bound at import and this process
    imported the module long ago.
    """
    env = dict(os.environ)
    env["DB_BACKEND"] = backend
    env["PROJECTS_DIR"] = env.get("PROJECTS_DIR", str(ROOT / ".pytest_projects"))
    return subprocess.run(
        [sys.executable, "-c", "import db.database as d; print(d.DB_BACKEND)"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def test_an_unknown_backend_refuses_to_import():
    r = _import_with("farbic")
    assert r.returncode != 0, (
        "A typo'd DB_BACKEND imported cleanly. It would fall back to the local "
        f"file silently. stdout={r.stdout!r}"
    )
    assert "DB_BACKEND" in r.stderr


def test_the_error_names_the_values_that_would_have_worked():
    r = _import_with("farbic")
    assert "sqlite" in r.stderr and "fabric" in r.stderr


def test_an_explicit_environment_value_beats_dotenv():
    """load_dotenv must not override a real export.

    This is what lets tests/conftest.py pin the suite to SQLite even while the
    developer's .env says fabric -- see the note there.
    """
    r = _import_with("sqlite")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "sqlite"


def test_case_and_whitespace_are_tolerated():
    r = _import_with("  FABRIC  ")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "fabric"


def test_the_suite_itself_is_pinned_to_sqlite():
    """The guard that keeps these tests off the live cloud database.

    Not hypothetical: when the local instance was first switched to Fabric, 13
    tests failed on IDENTITY_INSERT against the real database -- meaning the
    ones that did not fail had already written to it.
    """
    import db.database as database

    assert database.DB_BACKEND == "sqlite"
