from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

import skills_catalog

mcp = FastMCP("skills")


@mcp.tool()
def list_skills() -> str:
    """List the available Power BI skills (slug + when to use each). Call this
    FIRST to discover which skill fits the task, then load_skill(<slug>)."""
    return skills_catalog.list_skills_text()


@mcp.tool()
def load_skill(skill: str) -> str:
    """Load the full expert playbook for one skill by slug (e.g.
    'power-bi-dax'). Read and follow it before doing related work. A skill's
    reference files load with 'slug/reference-name' (e.g.
    'semantic-model-authoring/dax-guidelines')."""
    return skills_catalog.load_skill(skill)


if __name__ == "__main__":
    mcp.run()
