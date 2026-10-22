"""Claude Code is an inference provider here, not an agent.

FabricBI executes its own pbi_* tools. The CLI's built-in tools — shell,
filesystem, network, delegation — have no role in that and are denied.

This matters more than a normal hardening rule because of what feeds this path:
`report/frame.py` (Agent 1) and `model/project_requirements.py` send *uploaded
client documents* through it. Anything reachable from the model's tool surface
there is reachable by text someone else wrote.

The rule that failed before was duplication: `llm_client.py` restated the deny
list instead of importing it, and drifted five tools behind — MultiEdit
(a filesystem write), Artifact, Monitor, CronDelete and CronList were all
reachable, under `--permission-mode bypassPermissions`, which approves tool use
without asking. Nothing detected it because both lists looked deliberate.

These assertions read the built command line rather than running the CLI.
"""

from __future__ import annotations

import pytest

from config import CLAUDE_CLI_DISALLOWED_TOOLS


def _cmd(monkeypatch, **kwargs) -> list[str]:
    """Build the argv _stream_claude_cli would spawn, without spawning it."""
    import llm.llm_client as mod

    captured: dict = {}

    def _fake_spawn(cmd, prompt, env=None):
        captured["cmd"] = cmd
        raise RuntimeError("stop after argv construction")

    monkeypatch.setattr(mod, "_find_claude_bin", lambda: "claude")
    monkeypatch.setattr(mod, "_spawn_cli_process", _fake_spawn)

    gen = mod._stream_claude_cli(system="s", user="u", max_tokens=100, **kwargs)
    with pytest.raises(RuntimeError):
        list(gen) if hasattr(gen, "__iter__") else gen
    return captured["cmd"]


def _flag(cmd: list[str], name: str) -> str | None:
    return cmd[cmd.index(name) + 1] if name in cmd else None


# ---------------------------------------------------------------------------
# the deny list must be the one in config, always
# ---------------------------------------------------------------------------


def test_the_deny_list_is_config_not_a_local_copy(monkeypatch):
    """Every tool config denies must appear on the command line.

    A second hand-maintained list is what allowed MultiEdit through; this
    fails the moment one reappears.
    """
    denied = set((_flag(_cmd(monkeypatch), "--disallowedTools") or "").split(","))
    missing = set(CLAUDE_CLI_DISALLOWED_TOOLS) - denied
    assert not missing, f"config denies these but the CLI call does not: {sorted(missing)}"


@pytest.mark.parametrize(
    "tool",
    ["Bash", "PowerShell", "Edit", "Write", "MultiEdit", "Read", "Agent", "Workflow"],
)
def test_specific_dangerous_tools_are_denied_by_default(monkeypatch, tool):
    denied = (_flag(_cmd(monkeypatch), "--disallowedTools") or "").split(",")
    assert tool in denied


def test_read_is_the_only_tool_a_vision_caller_unlocks(monkeypatch):
    """allow_read exists for image input; it must not unlock anything else."""
    cmd = _cmd(monkeypatch, allow_read=True)
    denied = (_flag(cmd, "--disallowedTools") or "").split(",")
    assert "Read" not in denied
    assert _flag(cmd, "--allowedTools") == "Read"
    for tool in ("Bash", "Edit", "Write", "MultiEdit", "Workflow"):
        assert tool in denied


# ---------------------------------------------------------------------------
# permissions must fail closed when no tools were asked for
# ---------------------------------------------------------------------------


def test_pure_inference_does_not_bypass_permissions(monkeypatch):
    """The default path grants nothing and pre-approves nothing.

    bypassPermissions means a tool absent from the deny list runs silently.
    Combined with a deny list that had drifted, that is precisely how the
    exposure went unnoticed — so the default path must not use it.
    """
    cmd = _cmd(monkeypatch)
    assert "bypassPermissions" not in cmd
    assert _flag(cmd, "--permission-mode") == "default"
    assert "--allowedTools" not in cmd


def test_permissions_are_bypassed_only_when_tools_were_requested(monkeypatch):
    """Deliberate tool use still works — app.py's MCP surface depends on it."""
    cmd = _cmd(monkeypatch, allowed_tools="mcp__pbi__pbi_visual_list")
    assert _flag(cmd, "--permission-mode") == "bypassPermissions"
    assert _flag(cmd, "--allowedTools") == "mcp__pbi__pbi_visual_list"


def test_an_explicit_deny_list_from_the_caller_is_respected(monkeypatch):
    cmd = _cmd(monkeypatch, disallowed_tools="Bash,Write")
    assert _flag(cmd, "--disallowedTools") == "Bash,Write"
