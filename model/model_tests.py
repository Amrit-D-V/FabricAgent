from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

from config import FABRIC_WORKSPACE, XMLA_ENDPOINT_BASE
from fabric.pbi_xmla import _conn

DEFAULT_REL_TOLERANCE = 1e-6


def _dax_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE()" if value else "FALSE()"
    if isinstance(value, (int, float)):
        return repr(value)
    return '"' + str(value).replace('"', '""') + '"'


def _build_dax(test: dict) -> str:
    if test.get("dax"):
        return test["dax"]
    measure = test["measure"].strip().strip("[]")
    filters = test.get("filters") or {}
    if not filters:
        return f'EVALUATE ROW("Value", [{measure}])'
    parts = []
    for col, val in filters.items():
        if isinstance(val, list):
            members = ", ".join(_dax_literal(v) for v in val)
            parts.append(f"{col} IN {{{members}}}")
        else:
            parts.append(f"{col} = {_dax_literal(val)}")
    return f'EVALUATE ROW("Value", CALCULATE([{measure}], {", ".join(parts)}))'


def _parse_tolerance(test: dict, expected: Any) -> float:
    tol = test.get("tolerance")
    if tol is None:
        try:
            return abs(float(expected)) * DEFAULT_REL_TOLERANCE
        except (TypeError, ValueError):
            return 0.0
    if isinstance(tol, str) and tol.strip().endswith("%"):
        pct = float(tol.strip().rstrip("%")) / 100.0
        return abs(float(expected)) * pct
    return float(tol)


def _compare(actual: Any, expected: Any, tolerance: float) -> tuple[bool, str]:
    if expected is None:
        return actual is None, f"expected blank, got {actual!r}"
    try:
        a, e = float(actual), float(expected)
        ok = abs(a - e) <= tolerance
        return ok, f"actual={a:g} expected={e:g} (±{tolerance:g})"
    except (TypeError, ValueError):
        ok = str(actual) == str(expected)
        return ok, f"actual={actual!r} expected={expected!r}"


def _run_scalar(
    server: str, database: str, dax: str, *, roles: str = "", effective_user: str = ""
) -> Any:
    with _conn(server, database, roles=roles, effective_user=effective_user) as conn:
        cur = conn.cursor()
        cur.execute(dax)
        rows = cur.fetchall()
        cur.close()
    return rows[0][0] if rows else None


def run_spec(spec_path: str | Path) -> dict:
    try:
        spec = yaml.safe_load(Path(spec_path).read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"could not load spec: {e}"}
    if not isinstance(spec, dict) or not spec.get("tests"):
        return {"error": "spec must be a YAML mapping with a `tests` list"}

    model = spec.get("model", "")
    server = spec.get("server", "")
    if not server:
        workspace = spec.get("workspace") or FABRIC_WORKSPACE
        if not workspace or not model:
            return {"error": "spec needs `model` (+ `workspace`) or explicit `server`"}
        server = f"{XMLA_ENDPOINT_BASE}/{workspace}"
    database = spec.get("database", model)

    results: list[dict] = []
    for test in spec["tests"]:
        name = test.get("name") or test.get("measure") or "unnamed"
        entry: dict = {"name": name}
        try:
            dax = _build_dax(test)
            entry["dax"] = dax
            roles = str(test.get("role", "") or "")
            eff_user = str(test.get("effective_user", "") or "")
            if roles:
                entry["role"] = roles
            if eff_user:
                entry["effective_user"] = eff_user
            actual = _run_scalar(
                server, database, dax, roles=roles, effective_user=eff_user
            )
            expected = test.get("expect")
            tolerance = _parse_tolerance(test, expected)
            ok, detail = _compare(actual, expected, tolerance)
            entry.update({"passed": ok, "detail": detail})
        except KeyError as e:
            entry.update({"passed": False, "detail": f"spec missing field: {e}"})
        except Exception as e:
            entry.update({"passed": False, "detail": f"{type(e).__name__}: {e}"})
        results.append(entry)

    passed = sum(1 for r in results if r["passed"])
    return {
        "model": database,
        "server": server,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
    }


def _main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv
    if len(args) != 1:
        print("Usage: python model_tests.py <spec.yaml> [--json]")
        return 2
    out = run_spec(args[0])
    if as_json:
        print(json.dumps(out, indent=2, default=str))
    elif out.get("error"):
        print(f"ERROR: {out['error']}")
    else:
        print(f"Model: {out['model']}  ({out['server']})")
        for r in out["results"]:
            mark = "PASS" if r["passed"] else "FAIL"
            rls = (
                f"  [role={r.get('role', '')} user={r.get('effective_user', '')}]"
                if r.get("role") or r.get("effective_user")
                else ""
            )
            print(f"  [{mark}] {r['name']}{rls}")
            print(f"         {r['detail']}")
        print(f"\n{out['passed']}/{out['total']} passed")
    if out.get("error"):
        return 2
    return 0 if out["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(_main())
