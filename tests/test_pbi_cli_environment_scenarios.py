"""Report commands use the app's CLI and do not auto-control Desktop."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from llm import pbi_tool_specs as specs


def test_cli_runs_as_a_module_under_the_current_interpreter_not_the_launcher(
    monkeypatch,
):
    monkeypatch.setattr(specs.importlib.util, "find_spec", lambda _: object())
    assert specs._pbi_cli() == [specs.sys.executable, "-m", "pbi_cli"]


def test_system_cli_remains_a_fallback_when_not_installed_in_python_env(monkeypatch):
    monkeypatch.setattr(specs.importlib.util, "find_spec", lambda _: None)
    assert specs._pbi_cli() == ["pbi"]


def test_dispatcher_disables_desktop_sync_and_identifies_timed_out_operation(
    monkeypatch, tmp_path, no_hallucination_log
):
    from llm import pbi_tool_exec as executor
    from model import tool_guards

    monkeypatch.setattr(
        tool_guards, "_ALLOWED_PATH_ROOTS", (os.path.realpath(tmp_path),)
    )

    captured = []

    def timeout(cmd, **kwargs):
        captured.append(cmd)
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(executor.subprocess, "run", timeout)
    output = executor._execute_pbi_tool_impl(
        "pbi_report_create",
        {
            "path": str(tmp_path),
            "name": "QA",
            "model_name": "QA",
            "dataset_id": "12345678-1234-4234-8234-123456789abc",
        },
    )
    assert captured, output
    prefix = len(executor.PBI_CLI)
    assert captured[0][: prefix + 3] == [*executor.PBI_CLI, "report", "--no-sync", "create"]
    assert "pbi_report_create timed out" in output
    assert "partial" in output


def test_installed_cli_can_create_and_inspect_a_scratch_report(tmp_path):
    pytest.importorskip("pbi_cli")
    if specs.PBI_CLI == ["pbi"]:
        pytest.skip("pbi_cli is not importable in this Python environment")
    target = tmp_path / "report with spaces"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    created = subprocess.run(
        [
            *specs.PBI_CLI,
            "--json",
            "report",
            "--no-sync",
            "create",
            str(target),
            "--name",
            "QA & Overview",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
        env=env,
    )
    assert created.returncode == 0, created.stderr
    assert json.loads(created.stdout)["status"] == "created"
    report = target / "QA & Overview.Report"
    assert (report / "definition" / "report.json").is_file()
    inspected = subprocess.run(
        [*specs.PBI_CLI, "--json", "report", "--path", str(report), "info"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
        env=env,
    )
    assert inspected.returncode == 0, inspected.stderr
    assert json.loads(inspected.stdout)["total_visuals"] == 0


def _capture_cli(monkeypatch, tmp_path, tool, extra=None):
    from llm import pbi_tool_exec as executor
    from model import tool_guards

    monkeypatch.setattr(
        tool_guards, "_ALLOWED_PATH_ROOTS", (os.path.realpath(tmp_path),)
    )
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        assert kwargs.get("stdin") is subprocess.DEVNULL, (
            "a Python child that inherits the MCP stdio pipe blocks at "
            "interpreter start-up; the CLI must always get stdin=DEVNULL"
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    payload = {"path": str(tmp_path)}
    payload.update(extra or {})
    executor._execute_pbi_tool_impl(tool, payload)
    return seen


@pytest.mark.parametrize(
    "tool, extra",
    [
        ("pbi_report_info", None),
        ("pbi_visual_list", {"page": "overview"}),
        ("pbi_report_validate", None),
    ],
)
def test_read_only_report_commands_never_wake_power_bi_desktop(
    monkeypatch, tmp_path, tool, extra, no_hallucination_log
):
    for cmd in _capture_cli(monkeypatch, tmp_path, tool, extra):
        assert "--no-sync" in cmd, cmd


def test_reload_is_the_one_command_that_does_drive_desktop(
    monkeypatch, tmp_path, no_hallucination_log
):
    for cmd in _capture_cli(monkeypatch, tmp_path, "pbi_report_reload"):
        assert "--no-sync" not in cmd, cmd


def test_the_scaffold_command_detaches_stdin_from_the_mcp_pipe(
    monkeypatch, tmp_path, no_hallucination_log
):
    cmds = _capture_cli(
        monkeypatch, tmp_path, "pbi_report_create",
        {"name": "QA", "model_name": "QA", "dataset_id": "12345678-1234-4234-8234-123456789abc"},
    )
    assert cmds and cmds[0][-4:] == ["create", str(tmp_path), "--name", "QA"]
