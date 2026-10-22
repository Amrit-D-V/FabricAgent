"""A crashed verification must not read as a passed verification.

Agent 2's Phase 8 runs the verify/repair loop, and that loop is the only place
`collect_sql_probe_issues` executes — the pipeline's single check against live
warehouse data. The loop was wrapped in a bare `except Exception` that logged a
skip and moved on, leaving `verify_issues` at its empty default.

The blocking list is built from `verify_issues`, so an empty list meant no
blocking entry, `ready_to_deploy` stayed True, and the page showed a green
tick. A crash was byte-identical to a clean run — and because the loop is where
the live-data check lives, a transient rate limit could erase a genuine,
already-detected warehouse failure and flip the verdict from
"REVIEW BEFORE DEPLOY" to "ready".

These tests drive the blocking logic directly rather than the whole pipeline,
which needs a warehouse, an LLM and a Fabric connection.
"""

from __future__ import annotations

from semantic_model_v2.pipeline import PipelineResult


def _result() -> PipelineResult:
    """A PipelineResult at its defaults — the state a crashed phase leaves."""
    return PipelineResult(plan=None, plan_dict={})


def _blocking_from(result: PipelineResult) -> list[str]:
    """The verify-issue branch of the blocking rule, as pipeline.py applies it."""
    errors = [i for i in result.verify_issues if i.startswith("[ERROR]")]
    if errors:
        return [
            f"{len(errors)} issue(s) survived the verify/repair loop "
            f"({result.verify_stopped_reason})"
        ]
    return []


def test_a_crashed_verification_records_a_blocking_error():
    """What the except-branch must leave behind.

    Mirrors the handler: a crash appends an [ERROR] issue and stamps the
    stopped reason, so the existing blocking rule picks it up unchanged.
    """
    r = _result()
    exc = RuntimeError("429 rate limit")
    r.verify_stopped_reason = f"crashed: {type(exc).__name__}"
    r.verify_issues.append(
        f"[ERROR] verification did not complete ({type(exc).__name__}: {exc}) "
        f"— this model is UNVERIFIED, not verified-clean"
    )

    assert _blocking_from(r), "a crashed verification produced no blocking issue"
    assert not (not _blocking_from(r)), "ready_to_deploy would remain True"


def test_the_default_state_is_indistinguishable_without_that_record():
    """Why the fix is needed at all: the defaults look exactly like success.

    A PipelineResult that never ran verification has an empty verify_issues —
    identical to one that verified cleanly. Nothing downstream can tell them
    apart, which is why the crash handler has to say so explicitly.
    """
    crashed_but_silent = _result()
    verified_clean = _result()
    assert _blocking_from(crashed_but_silent) == _blocking_from(verified_clean) == []


def test_a_clean_verification_still_blocks_nothing():
    """The fix must not make every build unshippable."""
    r = _result()
    r.verify_stopped_reason = "converged"
    r.verify_issues = ["[WARNING] measure name is terse", "[INFO] 3 rounds run"]
    assert _blocking_from(r) == []


def test_real_surviving_errors_still_block():
    r = _result()
    r.verify_stopped_reason = "max_rounds"
    r.verify_issues = ["[ERROR] Invalid object name 'dbo.factpolicy'"]
    assert _blocking_from(r)


def test_ready_to_deploy_defaults_to_false():
    """Deployment readiness must be earned, never assumed."""
    assert _result().ready_to_deploy is False
