"""The contract between the declared tool surface and the dispatcher.

Two lists have to agree for the assistant to work at all: the specs in
`llm/pbi_tool_specs.py`, which are what the model is told it may call, and the
`tool_name` comparisons inside `_execute_pbi_tool_impl`, which are what actually
runs. Nothing enforced that agreement, and the two failure directions are
different bugs:

- declared but not dispatched -> the model calls a tool that silently falls
  through to the generic CLI shell-out at the end of the function, which is not
  what the spec promised;
- dispatched but not declared -> a handler nothing can ever reach, and its dead
  weight is indistinguishable from live code when reading the function.

These are checked by parsing, not by executing: the dispatcher needs Streamlit
session state, a Fabric connection and the `pbi` CLI, none of which exist here.

The read is AST-based on purpose. A `grep` for `tool_name == "..."` misses the
`tool_name in (...)` tuple forms -- which is how `pbi_model_inspect` and the six
`pbi_paginated_*` tools are dispatched -- and it also picks up comparisons in
`execute_pbi_tool`, the wrapper, which is a different function.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXEC_PY = ROOT / "llm" / "pbi_tool_exec.py"
SPECS_PY = ROOT / "llm" / "pbi_tool_specs.py"

# Names matched by more than one `if` in the dispatcher. Each of these is a
# two-stage dispatch: an early block that validates and returns ONLY on error,
# then the real handler further down. They are pinned by name so that a NEW
# double-match -- which would be an unreachable second handler -- fails here.
#
# 897  guards the pbi_paginated_* family's catalog/path resolution
# 1165 guards pbi_model_inspect + pbi_dax_query
# 1315 runs _validate_visual_bind_combo before pbi_visual_bind's handler at 1683
#
# The pbi_visual_* / pbi_card_set_image entries are double-matched because they
# are in _VISUAL_EDIT_TOOLS: the FORCE-LIST-BEFORE-EDIT guard matches first and
# returns only on a bad visual name, then the real handler runs. The last four
# were added when that guard was found not to cover them — see
# test_every_tool_that_edits_an_existing_visual_is_guarded below.
KNOWN_TWO_STAGE = {
    "pbi_dax_query",
    "pbi_paginated_delete",
    "pbi_paginated_get",
    "pbi_paginated_list",
    "pbi_paginated_set",
    "pbi_paginated_validate",
    "pbi_visual_bind",
    "pbi_visual_delete",
    "pbi_visual_format",
    "pbi_visual_set_container",
    "pbi_visual_update",
    "pbi_card_set_image",
    "pbi_visual_color_by_category",
    "pbi_visual_color_by_measure",
    "pbi_visual_conditional_color",
}

# Dispatched through a module-level constant rather than a literal, so the tuple
# members are not visible in the `if`. Resolved separately below.
_INDIRECT_SETS = {"_VISUAL_EDIT_TOOLS"}


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _tool_names_in_test(test: ast.expr) -> tuple[list[str], list[str]]:
    """(literal tool names, names of constants) this condition matches."""
    literal: list[str] = []
    indirect: list[str] = []
    for node in ast.walk(test):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "tool_name"):
            continue
        for op, comparator in zip(node.ops, node.comparators):
            if (
                isinstance(op, ast.Eq)
                and isinstance(comparator, ast.Constant)
                and isinstance(comparator.value, str)
            ):
                literal.append(comparator.value)
            elif isinstance(op, ast.In):
                if isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                    literal += [
                        e.value
                        for e in comparator.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    ]
                elif isinstance(comparator, ast.Name):
                    indirect.append(comparator.id)
    return literal, indirect


def _dispatch_sites() -> dict[str, list[int]]:
    """{tool_name: [line numbers of every `if` that can match it]}."""
    tree = _module(EXEC_PY)
    impl = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_execute_pbi_tool_impl"
    )
    sites: dict[str, list[int]] = {}
    for node in ast.walk(impl):
        if not isinstance(node, ast.If):
            continue
        literal, indirect = _tool_names_in_test(node.test)
        for name in literal:
            sites.setdefault(name, []).append(node.lineno)
        for const in indirect:
            for name in _resolve_constant(tree, const):
                sites.setdefault(name, []).append(node.lineno)
    # ast.walk is breadth-first, not source order -- a shallowly-nested `if`
    # later in the file can be visited before a deeply-nested one earlier in
    # the file, even though Python runs the function top-to-bottom at
    # runtime. Sort so "first site" reliably means "first in the file".
    return {name: sorted(lines) for name, lines in sites.items()}


GUARDS_PY = ROOT / "model" / "tool_guards.py"


def _resolve_constant(tree: ast.Module, name: str) -> list[str]:
    """String members of a module-level collection constant."""
    for candidate in (tree, _module(GUARDS_PY)):
        for node in candidate.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
                value = node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
                value = node.value
            else:
                continue
            if any(
                isinstance(t, ast.Name) and t.id == name for t in targets
            ) and isinstance(value, (ast.Tuple, ast.List, ast.Set)):
                return [
                    e.value
                    for e in value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
    return []


def _declared_tools() -> set[str]:
    """Every "name" a spec dict declares."""
    names: set[str] = set()
    for node in ast.walk(_module(SPECS_PY)):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "name"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                names.add(value.value)
    return names


# --- The two directions of drift ---------------------------------------------


def test_every_declared_tool_is_dispatched():
    """A spec with no handler sends the model down the generic CLI fallback."""
    missing = _declared_tools() - set(_dispatch_sites())
    assert (
        not missing
    ), f"declared in pbi_tool_specs.py but never dispatched: {sorted(missing)}"


def test_every_dispatched_tool_is_declared():
    """A handler for an undeclared name can never run. Delete it or declare it."""
    undeclared = set(_dispatch_sites()) - _declared_tools()
    assert (
        not undeclared
    ), f"dispatched in _execute_pbi_tool_impl but not declared: {sorted(undeclared)}"


# --- Structure the dispatcher relies on --------------------------------------


def test_no_new_tool_is_matched_by_two_conditions():
    """Guard-then-handler is fine; guard-then-handler-then-handler is dead code.

    The second handler for a name can never run, so a fix applied to it has no
    effect -- the failure mode is a change that appears to be made and is not.
    """
    doubled = {t: lns for t, lns in _dispatch_sites().items() if len(lns) > 1}
    assert set(doubled) == KNOWN_TWO_STAGE, (
        "double-matched tool names changed.\n"
        f"  now: {sorted(doubled)}\n"
        f"  pinned: {sorted(KNOWN_TWO_STAGE)}\n"
        "A new entry is probably an unreachable second handler. If it is a "
        "deliberate guard, add it to KNOWN_TWO_STAGE."
    )


@pytest.mark.parametrize("name", sorted(KNOWN_TWO_STAGE))
def test_a_two_stage_guard_runs_before_its_handler(name):
    """The guard must come first, or it cannot refuse anything."""
    lines = _dispatch_sites()[name]
    assert lines == sorted(lines) and len(lines) >= 2
    assert (
        lines[0] < lines[-1]
    ), f"{name}: guard at {lines[0]} is not before {lines[-1]}"


def test_the_visual_edit_guard_still_covers_the_editing_tools():
    """_VISUAL_EDIT_TOOLS drives the FORCE-LIST-BEFORE-EDIT refusal.

    It is the only thing stopping an edit to a visual name the model invented,
    so an editing tool dropping out of that set is a silent loss of the check --
    exactly what a refactor that extracts the per-tool handlers would do if it
    moved the handlers and left the guard behind.
    """
    covered = set(_resolve_constant(_module(EXEC_PY), "_VISUAL_EDIT_TOOLS"))
    assert covered, "_VISUAL_EDIT_TOOLS is no longer a module-level literal"
    for required in ("pbi_visual_bind", "pbi_visual_format", "pbi_visual_update"):
        assert required in covered, f"{required} lost its list-before-edit guard"


# ---------------------------------------------------------------------------
# every tool that edits an EXISTING visual must be guarded
# ---------------------------------------------------------------------------

# Tools that take a visual_name for a visual they are about to CREATE, so a
# name that does not exist yet is correct rather than a hallucination.
_VISUAL_CREATORS = {
    "pbi_visual_add",
    "pbi_visual_add_bound",
    "pbi_visual_add_image",
    "pbi_visual_add_button",
    "pbi_visual_add_page_nav",
}


def _tools_taking_visual_name() -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_module(SPECS_PY)):
        if not isinstance(node, ast.Dict):
            continue
        fields = {
            k.value: v
            for k, v in zip(node.keys, node.values)
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        name = fields.get("name")
        schema = fields.get("input_schema")
        if not (isinstance(name, ast.Constant) and schema is not None):
            continue
        if "visual_name" in ast.dump(schema):
            names.add(name.value)
    return names


def test_every_tool_that_edits_an_existing_visual_is_guarded():
    """FORCE-LIST-BEFORE-EDIT only protects tools listed in _VISUAL_EDIT_TOOLS.

    An unguarded editor can be pointed at a visual name the model invented.
    It then acts on nothing while returning its own success-shaped string, and
    the agent has no signal that the target never existed — the exact failure
    this guard was written to stop. Four tools
    (pbi_card_set_image, pbi_visual_color_by_category,
    pbi_visual_color_by_measure, pbi_visual_conditional_color) were missing.
    """
    guarded = set(_resolve_constant(_module(EXEC_PY), "_VISUAL_EDIT_TOOLS"))
    unclassified = _tools_taking_visual_name() - guarded - _VISUAL_CREATORS
    assert not unclassified, (
        f"{sorted(unclassified)} take a visual_name but are neither guarded as "
        "editors nor listed as creators. Add each to _VISUAL_EDIT_TOOLS in "
        "model/tool_guards.py, or to _VISUAL_CREATORS here if it creates the visual."
    )


def test_no_creator_is_wrongly_guarded_as_an_editor():
    """Guarding a creator would refuse every legitimate first call."""
    guarded = set(_resolve_constant(_module(EXEC_PY), "_VISUAL_EDIT_TOOLS"))
    assert not (guarded & _VISUAL_CREATORS)
