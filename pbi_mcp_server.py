"""The Power BI modeling tools, exposed over MCP.

This used to install a 63-line MagicMock of the whole `streamlit` module into
sys.modules before importing anything, because llm.pbi_tool_exec read
``st.session_state`` at import time and there is no script run out here. That
module now reads :mod:`core.context` instead, which works headlessly, so the
mock is gone: the server imports the real code and nothing else.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from auth.tool_access import tool_allowed
from llm.mcp_tools import register
from llm.pbi_tool_specs import PBI_TOOLS

mcp = FastMCP("pbi")

register(mcp, [spec for spec in PBI_TOOLS if tool_allowed(spec["name"])])

if __name__ == "__main__":
    mcp.run()
