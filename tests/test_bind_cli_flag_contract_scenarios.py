"""Which bind roles go to the CLI, and which are bound in-process.

`pbi visual bind` defines flags for only seven roles. Passing any other role
as `--<role>` is not a soft failure: Click exits 2 on an unknown option, so the
process dies before binding ANYTHING — the category and value in the same call
are lost too. The tool result is a Click usage dump, which reads as FabricBI
malfunctioning rather than "that role isn't wired up".

That is how `matrix→row+value+column`, `kpi→indicator+trend` and
`waterfall→category+value+breakdown` — three recipes `pbi_tool_specs.py`
advertises verbatim — failed on every single invocation.

These assertions are read off the source rather than executed, because the
dispatcher needs a Fabric connection and the real `pbi` binary.
"""

from __future__ import annotations

import ast
import inspect
import re

import llm.pbi_tool_exec as mod

# Declared by `bind` in pbi_cli/commands/visual.py. `--page` is passed
# separately and is not a data role.
CLI_BIND_FLAGS = {"category", "value", "row", "field", "legend", "indicator", "goal"}


def _bind_source() -> str:
    """Source of the pbi_visual_bind dispatch branch."""
    src = inspect.getsource(mod)
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_execute_pbi_tool_impl"
    )
    branches = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.If)
        and any(
            isinstance(c, ast.Constant) and c.value == "pbi_visual_bind"
            for c in ast.walk(n.test)
        )
    ]
    assert branches, "no pbi_visual_bind branch found"
    lines = src.splitlines()
    return "\n".join(
        "\n".join(lines[b.lineno - 1 : (b.end_lineno or b.lineno)]) for b in branches
    )


def _backend_only_roles() -> set[str]:
    src = _bind_source()
    m = re.search(r"_BACKEND_ONLY_ROLES\s*=\s*\((.*?)\)", src, re.S)
    assert m, "_BACKEND_ONLY_ROLES tuple not found in the bind branch"
    return set(re.findall(r'"([a-z_]+)"', m.group(1)))


def test_roles_without_a_cli_flag_are_bound_in_process():
    """Every role the CLI cannot accept must be routed to the backend."""
    backend = _backend_only_roles()
    for role in ("column", "trend", "breakdown", "line", "x", "y", "detail", "size"):
        assert role in backend, (
            f"'{role}' has no --{role} flag on `pbi visual bind`; forwarding it "
            "makes Click exit 2 and aborts the whole bind, losing the other "
            "roles in the same call. It must be in _BACKEND_ONLY_ROLES."
        )


def test_no_backend_only_role_is_also_a_real_cli_flag():
    """Guard against routing a role in-process that the CLI handles fine."""
    overlap = _backend_only_roles() & CLI_BIND_FLAGS
    assert not overlap, (
        f"{sorted(overlap)} are real CLI flags — routing them in-process skips "
        "the CLI's own handling for no reason."
    )


def test_the_cli_roles_present_check_lists_only_real_flags():
    """A role in this check but absent from the flag loop builds an empty command.

    `_cli_roles_present` decides whether to shell out at all. If it counts a
    role the CLI has no flag for, a bind carrying only that role takes the CLI
    path and runs `pbi visual bind <name> --page <p>` with no roles attached.
    """
    src = _bind_source()
    m = re.search(r"_cli_roles_present\s*=\s*any\((.*?)\n\s*\)\n", src, re.S)
    assert m, "_cli_roles_present not found"
    listed = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    assert listed <= CLI_BIND_FLAGS, (
        f"{sorted(listed - CLI_BIND_FLAGS)} are checked as CLI roles but the CLI "
        "has no such flag."
    )
