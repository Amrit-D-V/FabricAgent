from __future__ import annotations

import json

MODELING_TOOLS = frozenset(
    {
        "measure_operations",
        "column_operations",
        "relationship_operations",
        "table_operations",
        "security_role_operations",
        "user_hierarchy_operations",
        "calendar_operations",
        "calculation_group_operations",
        "perspective_operations",
        "culture_operations",
        "model_operations",
    }
)

_MUTATING_OPS = frozenset(
    {
        "create",
        "update",
        "delete",
        "rename",
        "activate",
        "deactivate",
        "reorder",
        "createpermission",
        "updatepermission",
        "deletepermission",
        "save",
        "add",
        "remove",
        "set",
    }
)

_PREFIXES = ("mcp__powerbi_modeling__", "mcp__pbi__")


def _bare(tool_name: str) -> str:
    for p in _PREFIXES:
        if tool_name.startswith(p):
            return tool_name[len(p) :]
    return tool_name


def normalize_mcp_request(args: dict) -> dict:
    if not isinstance(args, dict):
        return {"request": {}}
    if "request" not in args:
        return {"request": dict(args)}
    req = args["request"]
    if isinstance(req, str):
        try:
            req = json.loads(req)
        except (ValueError, TypeError):
            req = {}
        return {"request": req if isinstance(req, dict) else {}}
    return args


def classify_modeling_op(tool_name: str, args: dict) -> str:
    name = _bare(tool_name or "")
    if name not in MODELING_TOOLS:
        return "other"
    op = ""
    req = (args or {}).get("request")
    if isinstance(req, dict):
        op = str(req.get("operation", "")).strip().lower()
    elif isinstance(args, dict):
        op = str(args.get("operation", "")).strip().lower()
    return "mutate" if any(op.startswith(v) for v in _MUTATING_OPS) else "read"


def validate_home_table(
    object_type: str, table: str, col_reg: dict, mea_reg: dict
) -> str | None:
    if object_type not in ("measure", "column"):
        return None
    keys = [k for reg in (col_reg or {}, mea_reg or {}) for k in reg if k != "_meta"]
    if not keys:
        return None
    known_hint = sorted({k.rpartition(".")[0] for k in keys if "." in k})
    if not table:
        return (
            "ERROR — `table` is required for a measure/column proposal. "
            f"Valid tables: {', '.join(known_hint)}"
        )
    if not any(k.startswith(table + ".") for k in keys):
        return (
            f"ERROR — table '{table}' does not exist in the connected model, so "
            "this edit would fail at apply time. Pick the correct home table "
            f"from the model metadata and re-propose. Valid tables: "
            f"{', '.join(known_hint)}"
        )
    return None
