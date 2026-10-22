from __future__ import annotations


def _q(table: str) -> str:
    return "'" + table.replace("'", "''") + "'"


def _col(table: str, col: str) -> str:
    return f"{_q(table)}[{col.replace(']', ']]')}]"


def generate_assertions(metadata: dict) -> list[dict]:
    assertions: list[dict] = []
    for from_t, from_c, to_t, to_c, active in metadata.get("relationships", []):
        if not active or "?" in (from_t, from_c, to_t, to_c):
            continue
        assertions.append(
            {
                "kind": "rel_integrity",
                "name": f"{from_t}[{from_c}] -> {to_t}[{to_c}]",
                "dax": (
                    "EVALUATE ROW("
                    f'"unmatched", COUNTROWS(EXCEPT(VALUES({_col(from_t, from_c)}), '
                    f"VALUES({_col(to_t, to_c)}))), "
                    f'"total", DISTINCTCOUNT({_col(from_t, from_c)}))'
                ),
            }
        )
    for err in metadata.get("measure_errors", []):
        assertions.append(
            {"kind": "measure_error", "name": err.split(":")[0], "detail": err}
        )
    return assertions


def run_assertions(server: str, database: str, metadata: dict) -> dict:
    assertions = generate_assertions(metadata)
    errors: list[dict] = []
    warnings: list[dict] = []
    passed = 0

    for a in assertions:
        if a["kind"] == "measure_error":
            errors.append(
                {
                    "assertion": a["name"],
                    "kind": a["kind"],
                    "message": a["detail"],
                }
            )

    dax_assertions = [a for a in assertions if a["kind"] == "rel_integrity"]
    if dax_assertions:
        try:
            from fabric.pbi_xmla import _conn

            with _conn(server, database) as conn:
                for a in dax_assertions:
                    try:
                        cur = conn.cursor()
                        cur.execute(a["dax"])
                        rows = cur.fetchall()
                        cur.close()
                        row = rows[0]
                    except Exception as exc:
                        warnings.append(
                            {
                                "assertion": a["name"],
                                "kind": a["kind"],
                                "message": f"assertion query failed: {exc}",
                            }
                        )
                        continue
                    unmatched = int(row[0] or 0)
                    total = int(row[1] or 0)
                    if total and unmatched >= total:
                        errors.append(
                            {
                                "assertion": a["name"],
                                "kind": a["kind"],
                                "message": (
                                    f"relationship joins NOTHING — all {total} key "
                                    "values on the from-side have no match on the "
                                    "to-side (wrong key or wrong column)."
                                ),
                            }
                        )
                    elif unmatched:
                        warnings.append(
                            {
                                "assertion": a["name"],
                                "kind": a["kind"],
                                "message": (
                                    f"{unmatched} of {total} key values have no "
                                    "match on the to-side (RI violation — rows land "
                                    "in the blank member)."
                                ),
                            }
                        )
                    else:
                        passed += 1
        except Exception as exc:
            return {
                "available": False,
                "note": f"XMLA connection failed: {exc}",
                "errors": errors,
                "warnings": warnings,
                "passed": passed,
            }

    return {
        "available": True,
        "errors": errors,
        "warnings": warnings,
        "passed": passed,
        "summary": (
            f"{passed} passed · {len(errors)} errors · {len(warnings)} warnings "
            f"({len(dax_assertions)} relationship checks)"
        ),
    }


def format_findings(result: dict) -> str | None:
    if not result or not result.get("available"):
        return None
    errors = result.get("errors") or []
    warnings = result.get("warnings") or []
    if not errors and not warnings:
        return None
    lines = [f"- ERROR [{e['assertion']}]: {e['message']}" for e in errors]
    lines += [f"- WARNING [{w['assertion']}]: {w['message']}" for w in warnings]
    return (result.get("summary", "") + "\n" + "\n".join(lines)).strip()
