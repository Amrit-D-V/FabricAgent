from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from config import (
    BPA_RULES_PATH,
    FABRIC_WORKSPACE,
    LINT_TIMEOUT_S,
    PBI_INSPECTOR_PATH,
    PBI_INSPECTOR_RULES_PATH,
    TE2_PATH,
    XMLA_ENDPOINT_BASE,
)
from util.logger_setup import get_logger

logger = get_logger(__name__)


def inspector_available() -> bool:
    return PBI_INSPECTOR_PATH.is_file() and PBI_INSPECTOR_RULES_PATH.is_file()


def te2_available() -> bool:
    return Path(TE2_PATH).is_file() and BPA_RULES_PATH.is_file()


def lint_report(report_path: str) -> dict:
    if not inspector_available():
        return {
            "available": False,
            "note": f"PBI-Inspector not found at {PBI_INSPECTOR_PATH}",
        }

    report_dir = Path(report_path)
    if not report_dir.is_dir():
        return {"available": True, "error": f"report path not found: {report_path}"}

    with tempfile.TemporaryDirectory(prefix="pbi_inspect_") as out_dir:
        cmd = [
            str(PBI_INSPECTOR_PATH),
            "-pbipreport",
            str(report_dir),
            "-rules",
            str(PBI_INSPECTOR_RULES_PATH),
            "-formats",
            "Json",
            "-output",
            out_dir,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=LINT_TIMEOUT_S,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "available": True,
                "error": f"inspector timed out after {LINT_TIMEOUT_S}s",
            }
        except Exception as e:
            return {"available": True, "error": f"inspector failed to run: {e}"}

        json_files = sorted(Path(out_dir).rglob("*.json"))
        if not json_files:
            if "No test results found" in (proc.stdout or ""):
                return {
                    "available": True,
                    "engine": "pbi-inspector",
                    "errors": [],
                    "warnings": [],
                    "summary": "0 errors · 0 warnings (all checks passed)",
                }
            return {
                "available": True,
                "error": "inspector produced no JSON output",
                "stdout_tail": (proc.stdout or "")[-800:],
                "stderr_tail": (proc.stderr or "")[-400:],
            }

        results: list[dict] = []
        # A result file we cannot read is not a file with nothing in it. Dropping
        # it silently turned "the lint did not run" into "0 errors · 0 warnings",
        # which is the one answer this gate must never give by accident.
        unreadable: list[str] = []
        for jf in json_files:
            try:
                payload = json.loads(jf.read_text(encoding="utf-8-sig"))
            except Exception as e:
                logger.warning(
                    f"Unreadable PBI-Inspector result {jf.name}: {e}", exc_info=True
                )
                unreadable.append(f"{jf.name}: {type(e).__name__}: {e}")
                continue
            entries = (
                payload.get("Results", payload)
                if isinstance(payload, dict)
                else payload
            )
            if isinstance(entries, list):
                results.extend(e for e in entries if isinstance(e, dict))

        _LOGTYPE = {0: "info", 1: "warning", 2: "error"}

        def _norm(e: dict) -> dict:
            raw_sev = e.get("LogType", e.get("Severity"))
            if isinstance(raw_sev, int):
                sev = _LOGTYPE.get(raw_sev, "warning")
            else:
                sev = str(raw_sev or "warning").lower()
            out = {
                "rule": e.get("RuleName") or e.get("RuleId") or "",
                "severity": sev,
                "page": e.get("ParentDisplayName") or e.get("ParentName") or "",
            }
            actual = e.get("Actual")
            if isinstance(actual, list) and actual:
                out["offending_items"] = actual
            return out

        if unreadable and not results:
            return {
                "available": True,
                "error": (
                    f"inspector wrote {len(json_files)} result file(s), none of "
                    f"them readable — the report was NOT linted"
                ),
                "unreadable_results": unreadable,
            }

        failed = [_norm(e) for e in results if not e.get("Pass", False)]
        errors = [f for f in failed if f["severity"] == "error"]
        warnings = [f for f in failed if f["severity"] != "error"]
        result: dict[str, Any] = {
            "available": True,
            "engine": "pbi-inspector",
            "errors": errors,
            "warnings": warnings,
            "summary": f"{len(errors)} errors · {len(warnings)} warnings (deterministic rules; passes omitted)",
        }
        if unreadable:
            result["unreadable_results"] = unreadable
            result[
                "summary"
            ] += f" — INCOMPLETE: {len(unreadable)} result file(s) unreadable"
        return result


def _fabric_conn_str(workspace: str) -> str:
    from fabric.fabric_connection import get_fabric_connection
    from fabric.pbi_xmla import _build_conn_str

    return _build_conn_str(
        f"{XMLA_ENDPOINT_BASE}/{workspace}", "", get_fabric_connection()
    )


_TE2_VIOLATION = re.compile(r'^(.{1,200}?) violates rule "(.+)"\s*$')
_NOISE_OBJECTS = ("LocalDateTable_", "DateTableTemplate_")


def _rule_severities() -> tuple[dict[str, int], str]:
    try:
        rules = json.loads(BPA_RULES_PATH.read_text(encoding="utf-8-sig"))
        return {r.get("Name", ""): int(r.get("Severity", 2)) for r in rules}, ""
    except Exception as e:
        logger.warning(
            f"Could not read BPA rule severities from {BPA_RULES_PATH}: {e}",
            exc_info=True,
        )
        return {}, f"{type(e).__name__}: {e}"


def _lint_model_python(model: str, workspace: str, server: str) -> dict:
    from fabric import pbi_xmla
    from model import bpa_engine

    ws = workspace or FABRIC_WORKSPACE
    endpoint = server or f"{XMLA_ENDPOINT_BASE}/{ws}"
    try:
        meta = pbi_xmla.fetch_bpa_metadata(endpoint, model)
    except Exception as e:
        return {
            "available": False,
            "error": f"could not read model {model!r} over XMLA: {type(e).__name__}: {e}",
        }
    return bpa_engine.evaluate(meta)


def lint_model(
    model: str,
    workspace: str = "",
    server: str = "",
    engine: str = "auto",
) -> dict:
    engine = (engine or "auto").lower()
    if engine == "python":
        return _lint_model_python(model, workspace, server)
    if engine == "auto" and not te2_available():
        result = _lint_model_python(model, workspace, server)
        if result.get("available"):
            result["note"] = (
                f"Tabular Editor 2 not installed ({TE2_PATH}) — used FabricBI's "
                f"in-process rules. Install TE2 for Microsoft's full rule set."
            )
        return result
    if not te2_available():
        return {
            "available": False,
            "note": f"TE2 or rules missing (TE2={TE2_PATH}, rules={BPA_RULES_PATH})",
        }

    source = server or _fabric_conn_str(workspace or FABRIC_WORKSPACE)
    cmd = [TE2_PATH, source, model, "-A", str(BPA_RULES_PATH)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=LINT_TIMEOUT_S,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"available": True, "error": f"TE2 timed out after {LINT_TIMEOUT_S}s"}
    except Exception as e:
        return {"available": True, "error": f"TE2 failed to run: {e}"}

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    severities, severity_error = _rule_severities()
    by_rule: dict[str, list[str]] = {}
    for line in out.splitlines():
        m = _TE2_VIOLATION.match(line.strip())
        if not m:
            continue
        obj, rule = m.group(1).strip(), m.group(2).strip()
        if any(noise in obj for noise in _NOISE_OBJECTS):
            continue
        by_rule.setdefault(rule, []).append(obj)

    if proc.returncode != 0 and not by_rule:
        return {
            "available": True,
            "error": f"TE2 exit {proc.returncode}",
            "stdout_tail": out[-800:],
        }

    sev_name = {1: "info", 2: "warning", 3: "error"}
    findings = [
        {
            "rule": rule,
            "severity": sev_name.get(severities.get(rule, 2), "warning"),
            "objects": objs,
        }
        for rule, objs in sorted(by_rule.items())
    ]
    errors = [f for f in findings if f["severity"] == "error"]
    warnings = [f for f in findings if f["severity"] == "warning"]
    infos = [f for f in findings if f["severity"] == "info"]
    result: dict[str, Any] = {
        "available": True,
        "engine": "tabular-editor-2 + microsoft BPARules",
        "errors": errors,
        "warnings": warnings,
        "info": infos,
        "summary": (
            f"{len(errors)} error rules · {len(warnings)} warning rules · "
            f"{len(infos)} info rules ({sum(len(f['objects']) for f in findings)} objects flagged)"
        ),
    }
    if severity_error:
        result["severity_error"] = severity_error
        result["summary"] += (
            f" — SEVERITIES UNKNOWN ({BPA_RULES_PATH} unreadable: "
            f"{severity_error}); every rule defaulted to 'warning', so an error "
            f"rule may be listed as a warning"
        )
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3 or sys.argv[1] not in ("report", "model"):
        print("Usage: python bpa_lint.py report <report_folder>")
        print("       python bpa_lint.py model <model_name> [workspace]")
        sys.exit(2)
    if sys.argv[1] == "report":
        out = lint_report(sys.argv[2])
    else:
        out = lint_model(
            sys.argv[2], workspace=sys.argv[3] if len(sys.argv) > 3 else ""
        )
    print(json.dumps(out, indent=2))
    if out.get("error") or not out.get("available", True):
        sys.exit(2)
    sys.exit(1 if out.get("errors") else 0)
