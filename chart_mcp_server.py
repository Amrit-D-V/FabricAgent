"""The two visualisation tools, and nothing else.

The chat endpoint is reachable over the network, so its tool surface is kept
as small as the job allows. `pbi_mcp_server` registers every PBI tool --
including model edits -- which is more than a chat answer needs. This server
exposes only `render_chart` and `render_map`, so the assistant can draw a
result without also gaining the ability to change a model.

Both tools return a short confirmation to the model; the spec itself is
captured by execute_pbi_tool into bridge_io/session_<id>_tools.jsonl, which is
what api/chat.py reads back to send the chart to the browser.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from llm.mcp_tools import register
from llm.pbi_tool_specs import PBI_TOOLS

VISUAL_TOOLS = ("render_chart", "render_map")

mcp = FastMCP("charts")

_by_name = {t["name"]: t for t in PBI_TOOLS}
for tname in VISUAL_TOOLS:
    if tname not in _by_name:  # a rename upstream must fail loudly, not silently drop charts
        raise RuntimeError(f"{tname} is missing from PBI_TOOLS")

register(mcp, [_by_name[t] for t in VISUAL_TOOLS])

if __name__ == "__main__":
    mcp.run()
