"""Registering the PBI tool specs on a FastMCP server.

Both MCP entry points (`pbi_mcp_server.py`, `chart_mcp_server.py`) expose tools
from the same hand-written specs in :mod:`llm.pbi_tool_specs`, so the awkward
part of the job lives here once rather than in each of them.

The awkward part: FastMCP builds a Pydantic model from the handler's
*signature* and validates every incoming call against it. A handler written as
``def handler(**kwargs)`` produces a model with one required field literally
named ``kwargs``, which does not match the schema the server advertises. Both
halves of that mismatch have been seen in production:

* a caller sending the advertised shape ``{"chart_type": ...}`` was rejected
  with ``kwargs Field required`` -- the tool never ran at all;
* a caller sending ``{"kwargs": {"chart_type": ...}}`` passed validation and
  arrived one level too deep, so every field read as missing and a chart drew
  as an empty box.

Overriding ``tool.parameters`` -- which the servers do, to advertise the rich
hand-written schema -- fixes only what is *advertised*; validation still runs
against the model built from the signature. So the signature is what has to
change. :func:`make_handler` attaches a synthesized ``__signature__`` naming
the schema's own fields, which is what FastMCP inspects, while the function
itself keeps ``**kwargs`` and accepts whatever validation hands back.
"""

from __future__ import annotations

import inspect
import keyword
from typing import Any

from llm.pbi_tool_exec import execute_pbi_tool


# The field a nested payload arrives under; see signature_for.
WRAPPER = "kwargs"


def signature_for(schema: dict) -> inspect.Signature | None:
    """A keyword-only signature naming each property in `schema`.

    Everything is annotated ``Any`` and defaulted: this exists to make
    FastMCP's generated model accept the advertised field *names*, not to
    re-validate a schema it is already sent. Returns None when a property
    cannot be a parameter name, in which case the caller keeps the
    ``**kwargs`` signature rather than registering something subtly wrong.
    """
    properties = schema.get("properties") or {}
    params = []
    for name in properties:
        if not name.isidentifier() or keyword.iskeyword(name) or name.startswith("_"):
            return None
        params.append(
            inspect.Parameter(
                name, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Any
            )
        )
    if not params:
        return None
    # Optional wrapper unwrap fallback
    params.append(
        inspect.Parameter(
            WRAPPER, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Any
        )
    )
    return inspect.Signature(params)


def make_handler(tool_name: str, schema: dict | None = None):
    def handler(**kwargs) -> str:
        wrapped = kwargs.pop(WRAPPER, None)
        if isinstance(wrapped, dict) and not any(
            v is not None for v in kwargs.values()
        ):
            kwargs = wrapped
        return execute_pbi_tool(
            tool_name, {k: v for k, v in kwargs.items() if v is not None}
        )

    if schema is not None:
        signature = signature_for(schema)
        if signature is not None:
            handler.__signature__ = signature  # type: ignore[attr-defined]
    return handler


def register(mcp, specs) -> None:
    """Register every spec in `specs` on `mcp`."""
    for spec in specs:
        name = spec["name"]
        mcp.add_tool(
            make_handler(name, spec["input_schema"]),
            name=name,
            description=spec["description"],
        )
        mcp._tool_manager._tools[name].parameters = spec["input_schema"]
