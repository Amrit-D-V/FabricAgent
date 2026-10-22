"""Shared fixtures for the scenario suites.

These tests are deliberately NOT linear: each module is probed across its whole
input space -- happy path, boundary, malformed, adversarial and
wrong-order-of-operations -- rather than once down the middle. Everything here
runs offline: no Fabric, no XMLA, no LLM, no network, no writes outside tmp_path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# config.py refuses to import without PROJECTS_DIR, and rightly so -- there is no
# safe default for where every project lives. But the suite must not depend on a
# developer's .env to collect, or CI cannot run it at all. setdefault, so a real
# local value still wins.
os.environ.setdefault("PROJECTS_DIR", str(ROOT / ".pytest_projects"))

# Pin the suite to SQLite. This is an assignment, NOT a setdefault: a real
# DB_BACKEND=fabric in .env must lose here. It is read at db.database import
# time, so it has to be set before anything imports that module.
#
# Not hypothetical. When the local instance was switched to Fabric, 13 tests
# started writing to the live cloud database and failed on IDENTITY_INSERT --
# which means the ones that did not fail had already written to it. The
# docstring above promises this suite touches no Fabric; this is what keeps
# that true.
os.environ["DB_BACKEND"] = "sqlite"


@pytest.fixture
def no_hallucination_log(monkeypatch):
    """Keep registry-rejection tests from appending to logs/ and the audit DB."""
    import llm.pbi_tool_exec as X

    recorded: list[tuple[list, str]] = []
    monkeypatch.setattr(
        X, "_log_hallucination", lambda a, r, t: recorded.append((list(a), r))
    )
    return recorded


@pytest.fixture
def request_state():
    """An open request scope, which is where conversation state now lives.

    The dispatcher reads it for the model-choice gate. With no scope open the
    production code treats the absence as 'headless' -- which would make every
    gate scenario pass vacuously -- so the tests must open a real one.
    """
    from core.context import begin, end, state

    token = begin()
    yield state()
    end(token)


@pytest.fixture
def clean_env(monkeypatch):
    """Drop the ambient PBI_* overrides so precedence tests start from zero."""
    for k in ("PBI_ACTIVE_MODELS", "PBI_ACTIVE_WORKSPACE", "MCP_USE_NPX"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


@pytest.fixture(autouse=True)
def _no_stray_writes(tmp_path, monkeypatch):
    """Anything that reaches for a temp dir gets the per-test one."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    yield
