"""Scenario matrix for parsing what an LLM actually returns.

strip_json_fences and _coerce_json_object stand between raw model output and
every structured consumer in the build: the plan generator, the critic, the
verify/repair loop, the refine patcher and the tool dispatcher. Models do not
return clean JSON down the middle -- they fence it, prefix it with prose,
double-encode it, truncate it, or emit a list when an object was asked for.
Each of those is a scenario here, not a footnote.
"""

from __future__ import annotations

import json

import pytest

from util.json_utils import fix_pbir_node, strip_json_fences

# --- The shapes a model actually emits around a JSON object -----------------

WRAPPERS = [
    ("bare object", '{"a": 1}'),
    ("json fence", '```json\n{"a": 1}\n```'),
    ("bare fence", '```\n{"a": 1}\n```'),
    ("uppercase lang tag", '```JSON\n{"a": 1}\n```'),
    ("wrong lang tag", '```python\n{"a": 1}\n```'),
    ("prose before and after", 'Here you go:\n```json\n{"a": 1}\n```\nHope that helps'),
    ("inline prose", 'Sure! {"a": 1} done.'),
    ("leading whitespace", '\n\n   {"a": 1}   \n'),
]


@pytest.mark.parametrize("raw", [w[1] for w in WRAPPERS], ids=[w[0] for w in WRAPPERS])
def test_object_survives_every_wrapper(raw):
    """Whatever the model wraps it in, json.loads must accept the result --
    that is the only contract the five call sites rely on."""
    assert json.loads(strip_json_fences(raw)) == {"a": 1}


def test_nested_structures_are_not_truncated_at_the_first_close_brace():
    raw = '```json\n{"a": {"b": [1, 2]}, "c": 3}\n```'
    assert json.loads(strip_json_fences(raw)) == {"a": {"b": [1, 2]}, "c": 3}


def test_a_brace_inside_a_string_value_does_not_end_the_object():
    """The clamp uses rfind('}'), so a '}' that is DATA still lands inside the
    slice. Correct here -- and the reason the next case is not."""
    assert json.loads(strip_json_fences('```json\n{"a": "}"}\n```')) == {"a": "}"}


def test_a_stray_brace_after_the_object_drags_prose_into_the_slice():
    """rfind takes the LAST '}' in the whole response, so trailing prose that
    happens to contain a brace is captured with it. Pinned: this is how a
    perfectly good object turns into a parse error."""
    mangled = strip_json_fences('{"a": 1}\n\nNote: use {} for an empty set.')
    assert mangled.startswith('{"a": 1}')
    with pytest.raises(json.JSONDecodeError):
        json.loads(mangled)


# --- Degenerate input: must not raise, callers own the parse failure --------


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "no json here", "I cannot help with that", "```json\n```"],
)
def test_unparseable_input_is_returned_not_raised(raw):
    """The loops treat a JSONDecodeError as 'repairer produced nothing' and
    stop cleanly. An exception out of the stripper would instead kill the
    build."""
    out = strip_json_fences(raw)
    assert isinstance(out, str)


def test_truncated_object_is_left_for_the_caller_to_reject():
    """A response cut off by max_tokens has no closing brace. It must come back
    as something json.loads rejects, not as silently-repaired half-data."""
    with pytest.raises(json.JSONDecodeError):
        json.loads(strip_json_fences('```json\n{"a": 1'))


def test_unicode_is_preserved():
    raw = '```json\n{"a": "caf\\u00e9"}\n```'
    assert json.loads(strip_json_fences(raw))["a"] == "café"


# --- Arrays: the documented blind spot --------------------------------------
# The first-brace/last-brace clamp is object extraction. Every current caller
# json.loads()es an object, so this is by design -- but it is silent, and it
# turns a top-level list into either a different object or invalid JSON.


def test_single_element_array_is_silently_unwrapped_to_its_element():
    assert strip_json_fences('[{"a": 1}]') == '{"a": 1}'


def test_multi_element_array_becomes_invalid_json():
    """Pinned deliberately: if a future prompt asks for a list, this is the
    failure it will produce -- not an empty result, but a parse error two
    layers away from the cause."""
    mangled = strip_json_fences('```json\n[{"a": 1}, {"b": 2}]\n```')
    assert mangled == '{"a": 1}, {"b": 2}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(mangled)


# --- The same clamp applied to DAX -------------------------------------------
# dax/dax_assistant.py:54 routes fenced DAX through strip_json_fences. DAX uses
# braces for table constructors -- {1}, IN {"a","b"}, VAR x = {1,2} -- and the
# JSON clamp keeps only the brace span, discarding the query around it.


def test_fenced_dax_without_braces_is_unharmed():
    from dax.dax_assistant import _clean_dax

    assert _clean_dax("```dax\nSUM(t[x])\n```") == "SUM(t[x])"
    assert _clean_dax("```\nDIVIDE([a],[b])\n```") == "DIVIDE([a],[b])"


def test_unfenced_dax_never_reaches_the_json_clamp():
    from dax.dax_assistant import _clean_dax

    assert _clean_dax("EVALUATE {1}") == "EVALUATE {1}"


CORRUPTED_DAX = [
    ("table constructor", "```dax\nEVALUATE {1}\n```", "{1}"),
    (
        "IN list",
        '```dax\nCALCULATE(SUM(t[x]), t[c] IN {"a","b"})\n```',
        '{"a","b"}',
    ),
    ("VAR list", "```dax\nVAR x = {1,2} RETURN COUNTROWS(x)\n```", "{1,2}"),
]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: _clean_dax (dax/dax_assistant.py:54) sends fenced DAX through "
        "strip_json_fences, whose first-{/last-} clamp is JSON object "
        "extraction. Any DAX containing a table constructor is truncated to "
        "the brace span -- IN {...} is common, and the corruption is silent. "
        "Delete this marker once _clean_dax strips fences without the clamp."
    ),
)
@pytest.mark.parametrize(
    "raw,corrupted",
    [(c[1], c[2]) for c in CORRUPTED_DAX],
    ids=[c[0] for c in CORRUPTED_DAX],
)
def test_fenced_dax_with_braces_survives(raw, corrupted):
    from dax.dax_assistant import _clean_dax

    out = _clean_dax(raw)
    assert out != corrupted, "DAX truncated to its brace span"
    assert "EVALUATE" in out or "CALCULATE" in out or "RETURN" in out


# --- _coerce_json_object: tool arguments arrive in three shapes -------------
# A dict, a JSON string, or a JSON string that was encoded twice (some SDK
# paths stringify an already-stringified argument).


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"a": 1}, {"a": 1}),
        ('{"a": 1}', {"a": 1}),
        ('"{\\"a\\": 1}"', {"a": 1}),
        ("{}", {}),
        ('{"a": {"b": 1}}', {"a": {"b": 1}}),
    ],
    ids=["dict", "json string", "double-encoded", "empty object", "nested"],
)
def test_tool_arguments_are_coerced_to_a_dict(value, expected):
    from llm.pbi_tool_exec import _coerce_json_object

    assert _coerce_json_object(value) == expected


@pytest.mark.parametrize(
    "value",
    [None, 123, "", "   ", "not json", "[1, 2]", '"plain"', [], 0, False],
    ids=[
        "none",
        "int",
        "empty",
        "whitespace",
        "prose",
        "json array",
        "json string scalar",
        "list",
        "zero",
        "false",
    ],
)
def test_non_object_arguments_coerce_to_none_rather_than_guessing(value):
    """None is the dispatcher's 'I could not read these arguments' signal. A
    partial guess here would reach a pbi_* tool as real input."""
    from llm.pbi_tool_exec import _coerce_json_object

    assert _coerce_json_object(value) is None


def test_coercion_unwraps_at_most_one_extra_layer():
    """Two layers is a known SDK quirk. Three is a malformed payload, and
    unwrapping forever would let a deeply nested string become arguments."""
    from llm.pbi_tool_exec import _coerce_json_object

    twice = json.dumps(json.dumps({"a": 1}))
    thrice = json.dumps(twice)
    assert _coerce_json_object(twice) == {"a": 1}
    assert _coerce_json_object(thrice) is None


# --- fix_pbir_node: the 'active' key strip ----------------------------------


def test_active_is_stripped_at_every_depth():
    doc = {
        "active": True,
        "visual": {"active": False, "items": [{"active": True, "keep": 1}]},
        "keep": "yes",
    }
    fix_pbir_node(doc)
    assert doc == {"visual": {"items": [{"keep": 1}]}, "keep": "yes"}


@pytest.mark.parametrize("node", [None, 1, "s", [], {}, [[]], [{"a": 1}]])
def test_fix_pbir_node_tolerates_any_node_type(node):
    fix_pbir_node(node)


def test_active_is_not_stripped_from_a_string_key_position():
    """Only dict KEYS named 'active' go. A value of 'active' stays."""
    doc = {"role": "active", "items": ["active"]}
    fix_pbir_node(doc)
    assert doc == {"role": "active", "items": ["active"]}
