"""Live DAX probe: telling "the engine approved it" apart from "the engine never answered".

The probe's job is to say whether the engine accepted an expression. Its
failure mode is not a crash — it is answering "yes" when it never got a reply.
An auth failure, a timeout, or an empty response all mean UNVERIFIED, and the
caller renders anything that is not an explicit failure as
"Live check: passed", so a fail-open here becomes a correctness claim the user
has no reason to doubt.

`validate_dax_live` takes a `runner`, so every case below is offline.
"""

from __future__ import annotations

import pytest

from model.dax_live_validate import validate_dax_live

ARGS = ("srv", "db", "measure", "Sales", "Total", "SUM(Sales[Amt])")


def _status(reply: str) -> str:
    _msg, status = validate_dax_live(
        *ARGS, runner=lambda *_a, **_k: reply, return_status=True
    )
    return status


# ---------------------------------------------------------------------------
# infra failures must never be reported as a pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "Unauthorized",
        "AADSTS700016: Application with identifier was not found",
        "The operation has timed out",
        "Forbidden",
        "authentication failed",
        "Max retries exceeded with url",
        "unable to connect to the server",
        "getaddrinfo failed",
    ],
)
def test_an_infra_failure_without_the_word_error_is_not_a_pass(reply):
    """These replies contain no "error" substring.

    The substring test used to run FIRST, so each of these was classified
    "ok" and surfaced as "Live check: passed" — the engine reporting it
    never ran, read back as the expression being valid.
    """
    assert _status(reply) == "infra"


def test_an_empty_response_is_not_a_pass():
    """Silence is not approval — an empty reply verifies nothing."""
    assert _status("") == "infra"
    assert _status("   \n  ") == "infra"


def test_an_infra_failure_that_does_say_error_is_still_infra():
    assert _status("DAX error: The operation has timed out") == "infra"


# ---------------------------------------------------------------------------
# real outcomes still classify correctly
# ---------------------------------------------------------------------------


def test_a_clean_result_is_a_pass():
    assert _status("__probe\n42") == "ok"


def test_a_genuine_dax_error_is_reported_as_a_failure():
    msg, status = validate_dax_live(
        *ARGS,
        runner=lambda *_a, **_k: "DAX error: The column 'Sales[Nope]' cannot be found",
        return_status=True,
    )
    assert status == "error"
    assert "cannot be found" in msg


def test_no_connection_details_means_skipped_not_passed():
    """Not being connected is a different answer from being validated."""
    _msg, status = validate_dax_live(
        "", "", "measure", "Sales", "Total", "SUM(Sales[Amt])", return_status=True
    )
    assert status == "skipped"


# ---------------------------------------------------------------------------
# the legacy contract callers depend on
# ---------------------------------------------------------------------------


def test_default_return_shape_is_unchanged_for_existing_callers():
    """propose/verify callers treat None as "nothing blocking".

    An infra hiccup must stay non-blocking for them even though debug mode
    now distinguishes it — the status is additive, not a behaviour change.
    """
    assert validate_dax_live(*ARGS, runner=lambda *_a, **_k: "Unauthorized") is None
    assert validate_dax_live(*ARGS, runner=lambda *_a, **_k: "__probe\n42") is None
    assert validate_dax_live(
        *ARGS, runner=lambda *_a, **_k: "DAX error: bad column"
    ).startswith("ERROR")
