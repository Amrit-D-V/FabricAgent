from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


_DEFAULT_TE2_PATHS = [
    r"C:\Program Files (x86)\Tabular Editor\TabularEditor.exe",
    r"C:\Program Files\Tabular Editor\TabularEditor.exe",
]


@dataclass
class BPAIssue:
    rule_id: str
    severity: str
    object_path: str
    message: str

    def __str__(self) -> str:
        return f"[BPA·{self.severity}] {self.rule_id} on `{self.object_path}` — {self.message}"


def _find_te2() -> str | None:
    env_path = os.getenv("TE2_PATH", "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path
    for p in _DEFAULT_TE2_PATHS:
        if os.path.isfile(p):
            return p
    return None


_VSTS_LINE = re.compile(r"##vso\[task\.logissue\s+type=(?P<sev>\w+);?\](?P<body>.+)")
_BODY_LINE = re.compile(
    r"^(?P<kind>\w+)\s+(?P<obj>.+?)\s+violates\s+rule\s+\"(?P<rule>[^\"]+)\"\s*$",
    re.IGNORECASE,
)


def _parse_te2_output(text: str) -> list[BPAIssue]:
    issues: list[BPAIssue] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _VSTS_LINE.match(line)
        if not m:
            continue
        sev = m.group("sev").lower()
        body = m.group("body").strip()
        if body.startswith("Done") or "task.complete" in body:
            continue

        m2 = _BODY_LINE.match(body)
        if m2:
            issues.append(
                BPAIssue(
                    rule_id=m2.group("rule"),
                    severity=sev,
                    object_path=f"{m2.group('kind')} {m2.group('obj').strip()}",
                    message=f"violates rule \"{m2.group('rule')}\"",
                )
            )
        else:
            issues.append(
                BPAIssue(
                    rule_id="(unparsed)",
                    severity=sev,
                    object_path="?",
                    message=body,
                )
            )
    return issues


def run_bpa(
    model_path: str,
    timeout_sec: int = 120,
) -> tuple[list[BPAIssue], str]:
    te2 = _find_te2()
    if not te2:
        return (
            [],
            "TabularEditor.exe not found — set TE2_PATH in .env or install Tabular Editor 2.",
        )

    rules = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bpa_rules.json")
    if not os.path.isfile(rules):
        return ([], f"BPA rules file not found at {rules}")

    if not os.path.exists(model_path):
        return ([], f"Model not found at {model_path}")

    if os.path.isdir(model_path) and os.path.basename(model_path).endswith(
        ".SemanticModel"
    ):
        sub = os.path.join(model_path, "definition")
        if os.path.isdir(sub):
            model_path = sub

    cmd = [te2, model_path, "-A", rules, "-V"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ([], f"BPA timed out after {timeout_sec}s")
    except Exception as e:
        return ([], f"TE2 launch failed: {e}")

    combined = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    issues = _parse_te2_output(combined)
    return (issues, combined)
