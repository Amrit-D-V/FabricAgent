"""Does the cached field registry belong to the model this report binds to?

Agent 2 builds the semantic model, Agent 3 authors the report. Both read the
same two cache files, which belong to whichever model was connected last — in
whatever project. Nothing carried the model's identity across that handoff.

The failure is quiet and expensive. The registry is what proposed
`Table[Field]` names are validated against, so a registry from another model
rejects fields that exist and accepts names that do not exist here. The report
builds, the visuals render, and the numbers are wrong.

This was not hypothetical: the registry on disk during development described
"DIAD Final Report" in workspace "FabricBI" while the active work was
"Sales & Profitability" in "FabricBI-test".

The check is deliberately asymmetric — see test_unknown_provenance_is_not_a_mismatch.
"""

from __future__ import annotations

import json

import pytest

from report import model_identity


def _report(tmp_path, catalog: str | None):
    """A .Report tree, optionally bound to `catalog`."""
    root = tmp_path / "X.Report"
    (root / "definition").mkdir(parents=True)
    ref = {}
    if catalog is not None:
        ref = {
            "datasetReference": {
                "byConnection": {
                    "connectionString": (
                        f'Data Source=powerbi://api.powerbi.com/v1.0/myorg/WS;'
                        f'initial catalog="{catalog}";'
                    )
                }
            }
        }
    (root / "definition.pbir").write_text(json.dumps(ref), encoding="utf-8")
    return str(root)


@pytest.fixture
def registry(monkeypatch):
    """Control what the cached registry claims to describe."""

    def _set(models, generated_at="2026-08-18T16:58:51"):
        monkeypatch.setattr(
            model_identity,
            "_load_registry_meta",
            lambda: {"model_names": list(models), "generated_at": generated_at},
        )

    return _set


# ---------------------------------------------------------------------------
# the case that silently corrupts a deliverable
# ---------------------------------------------------------------------------


def test_a_registry_for_another_model_is_reported(tmp_path, registry):
    registry(["DIAD Final Report"])
    msg = model_identity.registry_mismatch(_report(tmp_path, "Sales & Profitability"))
    assert msg is not None
    assert "DIAD Final Report" in msg
    assert "Sales & Profitability" in msg
    assert "pbi_refresh_registry" in msg, "must say how to fix it"


def test_the_message_names_when_the_registry_was_cached(tmp_path, registry):
    """Staleness is the usual cause, so the timestamp belongs in the message."""
    registry(["Other Model"], generated_at="2026-08-18T16:58:51")
    msg = model_identity.registry_mismatch(_report(tmp_path, "Live Model"))
    assert "2026-08-18" in msg


def test_a_matching_registry_reports_nothing(tmp_path, registry):
    registry(["Sales & Profitability"])
    assert model_identity.registry_mismatch(_report(tmp_path, "Sales & Profitability")) is None


def test_one_of_several_connected_models_still_matches(tmp_path, registry):
    """A registry may legitimately cover several models at once."""
    registry(["Model A", "Sales & Profitability", "Model C"])
    assert model_identity.registry_mismatch(_report(tmp_path, "Sales & Profitability")) is None


# ---------------------------------------------------------------------------
# asymmetry: unknown is not the same as wrong
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("catalog", [None, ""])
def test_unknown_provenance_is_not_a_mismatch(tmp_path, registry, catalog):
    """A report with no live connection must not be blocked.

    Only a POSITIVE disagreement is reported. Treating "cannot tell" as a
    mismatch would refuse legitimate work, and a guard that fires on normal
    usage gets switched off — costing more than the case it catches.
    """
    registry(["Some Model"])
    assert model_identity.registry_mismatch(_report(tmp_path, catalog)) is None


def test_an_empty_registry_is_not_a_mismatch(tmp_path, registry):
    """Emptiness is refused separately, by the bind validator, with its own message."""
    registry([])
    assert model_identity.registry_mismatch(_report(tmp_path, "Anything")) is None


def test_registry_is_for_requires_positive_proof(tmp_path, registry):
    """None from registry_mismatch means "no evidence", never "verified match"."""
    registry(["Some Model"])
    unknown = _report(tmp_path, None)
    assert model_identity.registry_mismatch(unknown) is None
    assert model_identity.registry_is_for(unknown) is False


# ---------------------------------------------------------------------------
# reading the report's own binding
# ---------------------------------------------------------------------------


def test_the_catalog_is_read_from_the_connection_string(tmp_path):
    assert model_identity.report_catalog(_report(tmp_path, "My Model")) == "My Model"


def test_an_unreadable_report_yields_no_catalog_rather_than_raising(tmp_path):
    root = tmp_path / "Broken.Report"
    (root / "definition").mkdir(parents=True)
    (root / "definition.pbir").write_text("{ not json", encoding="utf-8")
    assert model_identity.report_catalog(str(root)) == ""


def test_describe_states_the_verdict_plainly(tmp_path, registry):
    registry(["DIAD Final Report"])
    out = model_identity.describe(_report(tmp_path, "Sales & Profitability"))
    assert "MISMATCH" in out
    assert "DIAD Final Report" in out and "Sales & Profitability" in out
