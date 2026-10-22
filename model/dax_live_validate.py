from __future__ import annotations

_INFRA_MARKERS = (
    "not connected",
    "could not be resolved",
    "failed to resolve",
    "getaddrinfo",
    "unable to connect",
    "no such host",
    "could not open a connection",
    "hostname resolution",
    "temporary error",
    "timeout",
    "timed out",
    "the operation has timed out",
    "max retries",
    "service is unavailable",
    "unauthorized",
    "forbidden",
    "authentication failed",
    "aadsts",
)


def _q(table: str) -> str:
    return "'" + table.replace("'", "''") + "'"


def _bracket(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def build_probe_query(
    otype: str,
    table: str,
    name: str,
    definition: str,
    pending_measures=(),
) -> str:
    if not (definition or "").strip():
        return ""
    if otype == "measure":
        defines = [
            f"MEASURE {_q(pt)}{_bracket(pn)} = {pd}"
            for pt, pn, pd in pending_measures
            if (pd or "").strip()
        ]
        defines.append(f"MEASURE {_q(table)}{_bracket(name)} = {definition}")
        return (
            "DEFINE\n  "
            + "\n  ".join(defines)
            + f'\nEVALUATE ROW("__probe", {_bracket(name)})'
        )
    if otype == "column":
        return f'EVALUATE TOPN(1, ADDCOLUMNS({_q(table)}, "__probe", {definition}))'
    return ""


def validate_dax_live(
    server: str,
    database: str,
    otype: str,
    table: str,
    name: str,
    definition: str,
    pending_measures=(),
    runner=None,
    *,
    return_status: bool = False,
):

    def _ret(msg, status):
        return (msg, status) if return_status else msg

    if not server or not database:
        return _ret(None, "skipped")
    probe = build_probe_query(otype, table, name, definition, pending_measures)
    if not probe:
        return _ret(None, "skipped")
    if runner is None:
        from fabric.pbi_xmla import run_dax as runner

    result = runner(server, database, probe)
    head = result or ""
    for _cut in ("Technical Details", "\r\n   at ", "\n   at "):
        _i = head.find(_cut)
        if _i != -1:
            head = head[:_i]
    head_low = head.lower()

    # Treat empty response as infra failure rather than a pass
    if not head.strip():
        return _ret(None, "infra")

    # Check infrastructure errors (auth/timeouts) before generic error substring
    if any(m in head_low for m in _INFRA_MARKERS):
        return _ret(None, "infra")  # infra hiccup — never block a proposal on it

    if "error" not in (result or "").lower():
        return _ret(None, "ok")
    msg = head.split("error:", 1)[-1] if "error:" in head_low else head
    msg = msg.replace("<oii>", "").replace("</oii>", "")
    return _ret(
        "ERROR — live DAX validation failed: "
        + " ".join(msg.split())[:300]
        + " Fix the definition and re-propose.",
        "error",
    )
