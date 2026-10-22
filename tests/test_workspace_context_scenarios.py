"""Scenario matrix for the active workspace/model context.

Three sources can answer "which models are connected right now": a thread-local
snapshot adopted by a background worker, the PBI_ACTIVE_* environment
variables, and the module globals app.py pushes per rerun. They are checked in
that order, and getting the order wrong means a tool acts on a model the user
did not select -- which produces real-looking, wrong answers.

The registry files are fingerprinted by (workspace, models), so the same
scenarios decide which registry a bind is validated against.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

import fabric.workspace_context as WC


@pytest.fixture(autouse=True)
def isolate_context(monkeypatch):
    """Reset every source of truth: thread-local, env and module globals."""
    monkeypatch.delenv("PBI_ACTIVE_MODELS", raising=False)
    monkeypatch.delenv("PBI_ACTIVE_WORKSPACE", raising=False)
    if hasattr(WC._workspace_ctx_local, "ctx"):
        del WC._workspace_ctx_local.ctx
    saved = (WC.FABRIC_WORKSPACE, list(WC.FABRIC_MODELS), WC._ACTIVE_WF_PALETTE)
    WC.set_fabric_context("BaseWorkspace", ["base_model"])
    WC.set_active_wf_palette(None)
    WC._REGISTRY_JSON_CACHE.clear()
    yield
    WC.set_fabric_context(saved[0], saved[1])
    WC.set_active_wf_palette(saved[2])
    if hasattr(WC._workspace_ctx_local, "ctx"):
        del WC._workspace_ctx_local.ctx
    WC._REGISTRY_JSON_CACHE.clear()


@pytest.fixture
def no_live_lookup(monkeypatch):
    """_active_workspace calls get_fabric_connection to re-resolve a renamed
    workspace. Stub it so precedence tests never touch the DB or REST."""

    class _Conn:
        workspace = "__never_matches__"

        def live_workspace(self):  # pragma: no cover - must not be reached
            raise AssertionError("live_workspace called when it should not be")

    monkeypatch.setattr(WC, "_get_fc", lambda: _Conn())


# --- Model precedence -------------------------------------------------------


def test_module_globals_answer_when_nothing_overrides(no_live_lookup):
    assert WC._active_models() == ("base_model",)
    assert WC._active_workspace() == "BaseWorkspace"


def test_env_beats_the_module_globals(monkeypatch, no_live_lookup):
    monkeypatch.setenv("PBI_ACTIVE_MODELS", json.dumps(["env_model"]))
    monkeypatch.setenv("PBI_ACTIVE_WORKSPACE", "EnvWorkspace")
    assert WC._active_models() == ("env_model",)
    assert WC._active_workspace() == "EnvWorkspace"


def test_thread_local_override_beats_env_and_globals(monkeypatch, no_live_lookup):
    monkeypatch.setenv("PBI_ACTIVE_MODELS", json.dumps(["env_model"]))
    monkeypatch.setenv("PBI_ACTIVE_WORKSPACE", "EnvWorkspace")
    WC.adopt_workspace_context({"workspace": "SnapWorkspace", "models": ["snap_model"]})
    assert WC._active_models() == ("snap_model",)
    assert WC._active_workspace() == "SnapWorkspace"


def test_an_empty_override_still_wins(monkeypatch, no_live_lookup):
    """A worker that adopted a context with no models has genuinely no models.
    Falling through to the globals would silently re-attach the UI's
    selection."""
    monkeypatch.setenv("PBI_ACTIVE_MODELS", json.dumps(["env_model"]))
    WC.adopt_workspace_context({"workspace": "", "models": []})
    assert WC._active_models() == ()
    assert WC._active_workspace() == ""


@pytest.mark.parametrize(
    "raw",
    ["", "[]", "not json", "{}", "null"],
    ids=["empty", "empty list", "garbage", "object", "null"],
)
def test_unusable_env_value_falls_through_to_the_globals(
    monkeypatch, no_live_lookup, raw
):
    """A malformed PBI_ACTIVE_MODELS must not wipe the model list -- an empty
    tuple reads downstream as 'no model connected'."""
    monkeypatch.setenv("PBI_ACTIVE_MODELS", raw)
    assert WC._active_models() == ("base_model",)


def test_env_accepts_several_models(monkeypatch, no_live_lookup):
    monkeypatch.setenv("PBI_ACTIVE_MODELS", json.dumps(["a", "b", "c"]))
    assert WC._active_models() == ("a", "b", "c")


# --- Snapshot / adopt round trip --------------------------------------------


def test_snapshot_captures_all_three_fields(no_live_lookup):
    WC.set_active_wf_palette(["#111111"])
    snap = WC.snapshot_workspace_context()
    assert snap == {
        "workspace": "BaseWorkspace",
        "models": ("base_model",),
        "palette": ["#111111"],
    }


def test_adopting_a_snapshot_reproduces_it(no_live_lookup):
    WC.set_active_wf_palette(["#222222"])
    snap = WC.snapshot_workspace_context()
    WC.set_fabric_context("Different", ["other"])
    WC.set_active_wf_palette(None)
    WC.adopt_workspace_context(snap)
    assert WC._active_models() == ("base_model",)
    assert WC._active_workspace() == "BaseWorkspace"
    assert WC._active_palette() == ["#222222"]


def test_adopting_none_clears_to_an_empty_context(no_live_lookup):
    WC.adopt_workspace_context(None)
    assert WC._active_models() == ()
    assert WC._active_workspace() == ""
    assert WC._active_palette() is None


def test_an_adopted_context_does_not_leak_between_threads(no_live_lookup):
    """This is the whole reason it is a threading.local: two report jobs on two
    threads must not see each other's workspace."""
    WC.adopt_workspace_context({"workspace": "MainThread", "models": ["main"]})
    seen = {}

    def worker():
        seen["before_adopt"] = WC._active_models()
        WC.adopt_workspace_context({"workspace": "Worker", "models": ["worker"]})
        seen["after_adopt"] = WC._active_models()

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert seen["before_adopt"] == ("base_model",)
    assert seen["after_adopt"] == ("worker",)
    assert WC._active_models() == ("main",)


# --- Workspace rename resolution --------------------------------------------


def test_the_saved_connection_is_re_resolved_by_id(monkeypatch):
    """When the session is using the SAVED connection, the current name wins --
    Power BI reports a renamed workspace as 'not found', not 'denied'."""

    class _Conn:
        workspace = "BaseWorkspace"

        def live_workspace(self):
            return "BaseWorkspace-renamed"

    monkeypatch.setattr(WC, "_get_fc", lambda: _Conn())
    assert WC._active_workspace() == "BaseWorkspace-renamed"


def test_a_session_picked_workspace_is_returned_untouched(monkeypatch):
    """If the user chose a different workspace this session, that selection is
    not second-guessed against the saved connection."""

    class _Conn:
        workspace = "SomeOtherSavedWorkspace"

        def live_workspace(self):  # pragma: no cover
            raise AssertionError("should not re-resolve a session selection")

    monkeypatch.setattr(WC, "_get_fc", lambda: _Conn())
    assert WC._active_workspace() == "BaseWorkspace"


def test_a_failure_resolving_the_name_falls_back_to_the_stored_one(monkeypatch):
    def _boom():
        raise RuntimeError("no network")

    monkeypatch.setattr(WC, "_get_fc", _boom)
    assert WC._active_workspace() == "BaseWorkspace"


def test_an_empty_live_name_falls_back_to_the_stored_one(monkeypatch):
    class _Conn:
        workspace = "BaseWorkspace"

        def live_workspace(self):
            return ""

    monkeypatch.setattr(WC, "_get_fc", lambda: _Conn())
    assert WC._active_workspace() == "BaseWorkspace"


# --- Registry fingerprinting ------------------------------------------------


def test_model_order_does_not_change_the_registry_file(no_live_lookup):
    """['a','b'] and ['b','a'] are the same connection. A different path would
    silently orphan the registry and block every bind."""
    a = WC._registry_path("column", ("a", "b"))
    b = WC._registry_path("column", ("b", "a"))
    assert a == b


def test_a_different_model_set_gets_a_different_registry(no_live_lookup):
    assert WC._registry_path("column", ("a",)) != WC._registry_path(
        "column", ("a", "b")
    )


def test_a_different_workspace_gets_a_different_registry(monkeypatch, no_live_lookup):
    first = WC._registry_path("column", ("a",))
    monkeypatch.setenv("PBI_ACTIVE_WORKSPACE", "OtherWorkspace")
    assert WC._registry_path("column", ("a",)) != first


def test_measure_and_column_registries_do_not_collide(no_live_lookup):
    assert WC._registry_path("column", ("a",)) != WC._registry_path("measure", ("a",))


# --- Registry file reading --------------------------------------------------


def test_a_missing_registry_reads_as_empty(tmp_path):
    assert WC._read_registry_json(str(tmp_path / "nope.json")) == {}


def test_malformed_registry_json_reads_as_empty_not_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert WC._read_registry_json(str(p)) == {}


def test_the_registry_cache_is_invalidated_when_the_file_changes(tmp_path):
    """A refresh that writes a new registry must be picked up immediately;
    caching on path alone would keep serving the old field list."""
    p = tmp_path / "reg.json"
    p.write_text(json.dumps({"t.a": 1}), encoding="utf-8")
    assert WC._read_registry_json(str(p)) == {"t.a": 1}

    p.write_text(json.dumps({"t.b": 2}), encoding="utf-8")
    os.utime(str(p), (0, 12345))
    assert WC._read_registry_json(str(p)) == {"t.b": 2}


def test_meta_keys_are_stripped_from_the_loaded_registry(monkeypatch, tmp_path):
    reg = {"_meta": {"model_names": ["m"]}, "t.a": "int64", "t.b": "string"}
    path = tmp_path / "measure.json"
    path.write_text(json.dumps(reg), encoding="utf-8")
    monkeypatch.setattr(WC, "_read_registry", lambda kind: reg)
    assert WC._load_measure_registry() == {"t.a": "int64", "t.b": "string"}
    assert WC._load_column_registry() == {"t.a": "int64", "t.b": "string"}
    assert WC._load_registry_meta() == {"model_names": ["m"]}


def test_the_legacy_registry_is_accepted_only_for_a_matching_connection(
    monkeypatch, no_live_lookup, tmp_path
):
    """The legacy un-fingerprinted registry predates multi-connection support.
    Adopting it for the wrong workspace/models would validate binds against
    another client's schema."""
    legacy = {
        "_meta": {"model_names": ["base_model"], "workspace": "BaseWorkspace"},
        "t.a": "int64",
    }
    monkeypatch.setattr(WC, "PROJECT_ROOT", str(tmp_path))

    def fake_read(path):
        return legacy if path.endswith(".column_registry.json") else {}

    monkeypatch.setattr(WC, "_read_registry_json", fake_read)
    assert WC._read_registry("column") == legacy

    legacy["_meta"]["workspace"] = "SomeOtherWorkspace"
    assert WC._read_registry("column") == {}

    legacy["_meta"] = {"model_names": ["different"], "workspace": "BaseWorkspace"}
    assert WC._read_registry("column") == {}


def test_no_connected_models_never_falls_back_to_the_legacy_registry(
    monkeypatch, no_live_lookup
):
    """With nothing connected there is no connection to match the legacy meta
    against, so the legacy file must stay untouched rather than being adopted
    by default."""
    legacy = {
        "_meta": {"model_names": [], "workspace": ""},
        "t.a": "int64",
    }

    def fake_read(path):
        return legacy if path.endswith(".column_registry.json") else {}

    monkeypatch.setattr(WC, "_read_registry_json", fake_read)
    WC.adopt_workspace_context({"workspace": "", "models": []})
    assert WC._read_registry("column") == {}
