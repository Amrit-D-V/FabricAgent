from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from db.database import (
    COLUMN_CATALOG_SUFFIX,
    get_project,
    list_documents,
    requirements_revision,
)


@dataclass
class ProjectView:
    project: Any
    documents: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    user_email: str = ""

    @property
    def id(self) -> int:
        return self.project.id

    @property
    def name(self) -> str:
        return self.project.name

    @property
    def description(self) -> str:
        return self.project.description or ""

    def source(self, kind: str) -> dict | None:
        cfg = (self.metadata.get("sources") or {}).get(kind)
        if not isinstance(cfg, dict) or not cfg.get("server"):
            return None
        out = {k: v for k, v in cfg.items() if not k.endswith("_encrypted")}
        out["password_set"] = bool(cfg.get("password_encrypted"))
        return out

    def context_block(self, kind: str) -> str:
        return ((self.metadata.get("context_blocks") or {}).get(kind) or "").strip()

    def connected(self, kind: str) -> bool:
        return bool(self.source(kind) and self.context_block(kind))

    @property
    def has_sources(self) -> bool:
        return bool(
            self.documents or self.connected("sql") or self.connected("semantic_model")
        )

    @property
    def requirements(self) -> str:
        return (self.metadata.get("requirements") or "").strip()

    @property
    def requirements_rev(self) -> str:
        return requirements_revision(self.requirements)

    def column_catalog(self) -> list[str]:
        names: list[str] = []
        for kind in ("sql", "semantic_model"):
            raw = self.context_block(f"{kind}{COLUMN_CATALOG_SUFFIX}")
            if not raw:
                continue
            try:
                tables = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            for t in tables or []:
                table = (t or {}).get("name") or ""
                for col in (t or {}).get("columns") or []:
                    names.append(f"{table}.{col}" if table else str(col))
        return sorted(dict.fromkeys(names))


def load_project_view(project_id: int, user_email: str = "") -> ProjectView | None:
    proj = get_project(project_id)
    if not proj:
        return None
    try:
        documents = list_documents(project_id)
    except Exception:
        documents = []
    metadata = proj.metadata if isinstance(proj.metadata, dict) else {}
    return ProjectView(
        project=proj,
        documents=documents,
        metadata=metadata,
        user_email=user_email,
    )
