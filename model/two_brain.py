"""Two-Brain Semantic Model Architecture: Brain 1 (Planner) & Brain 2 (Compiler).

This module strictly decouples high-level semantic planning (Brain 1)
from deterministic TMDL compilation and PBIP layout (Brain 2).
Decoupling eliminates LLM hallucinations in physical TMDL files, ensures
predictable code generation, and allows users to review/edit abstract
model plans before any code is generated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from semantic_model_v2.plan import SchemaPlan, plan_to_dict, dict_to_plan
from semantic_model_v2.pipeline import PipelineResult, generate_model_v2

logger = logging.getLogger("fabricbi.two_brain")


@dataclass
class PlannerInput:
    """Inputs required by Brain 1 (Semantic Planner)."""
    schema_text: str
    schema_tables: dict[str, list[tuple[str, str]]]
    requirements: str
    model_name: str
    server_fqdn: str = ""
    db_name: str = ""
    profile: dict | None = None
    profile_context: str = ""
    rules_context: str = ""
    project_id: int | None = None
    kpi_requirements_block: str = ""
    reference_model_block: str = ""


class SemanticPlanner:
    """Brain 1: High-level semantic reasoning & star schema planning.

    Duties:
    - Interprets business goals and KPI specifications.
    - Classifies physical tables into Facts, Dimensions, and Bridges.
    - Generates high-level measure formulas and relationships.
    - Runs automated repair, critic verification, and rule validation.
    - Emits a clean, frozen SchemaPlan specification.
    """

    def __init__(self, input_spec: PlannerInput):
        self.input = input_spec

    def plan(self, progress_callback: Callable[[str], None] | None = None) -> SchemaPlan:
        """Run Brain 1 planning pipeline and return a strictly validated SchemaPlan."""
        if progress_callback:
            progress_callback("Brain 1 (Planner): Analyzing schema and requirements...")

        result: PipelineResult = generate_model_v2(
            schema_text=self.input.schema_text,
            schema_tables=self.input.schema_tables,
            profile=self.input.profile,
            profile_context=self.input.profile_context,
            requirements=self.input.requirements,
            model_name=self.input.model_name,
            server_fqdn=self.input.server_fqdn,
            db_name=self.input.db_name,
            rules_context=self.input.rules_context,
            project_id=self.input.project_id,
            kpi_requirements_block=self.input.kpi_requirements_block,
            reference_model_block=self.input.reference_model_block,
            approved_plan=None,  # Triggers Brain 1 phase
            progress_callback=progress_callback,
            verify_loop_enabled=False,
        )

        if not result.plan:
            err = result.error_message or "Planner failed to produce a valid schema plan."
            raise ValueError(f"Brain 1 Planning Failed: {err}")

        return result.plan


class DeterministicCompiler:
    """Brain 2: Zero-hallucination deterministic compiler.

    Duties:
    - Takes an approved, verified SchemaPlan from Brain 1 (or user review).
    - Compiles the plan directly into concrete TMDL definitions and partitions.
    - Injects standardized format strings, display folders, and type coercions.
    - Runs TMDL doctor, reference checks, and PBIP folder layout.
    - Zero LLM calls in this phase: 100% deterministic compilation.
    """

    def __init__(self, plan: SchemaPlan | dict, input_spec: PlannerInput):
        if isinstance(plan, dict):
            self.plan = dict_to_plan(plan)
        else:
            self.plan = plan
        self.input = input_spec

    def compile(
        self,
        progress_callback: Callable[[str], None] | None = None,
        verify: bool = True,
    ) -> PipelineResult:
        """Compile SchemaPlan into TMDL files and PBIP representation."""
        if progress_callback:
            progress_callback("Brain 2 (Compiler): Deterministically compiling TMDL files...")

        result: PipelineResult = generate_model_v2(
            schema_text=self.input.schema_text,
            schema_tables=self.input.schema_tables,
            profile=self.input.profile,
            profile_context=self.input.profile_context,
            requirements=self.input.requirements,
            model_name=self.input.model_name,
            server_fqdn=self.input.server_fqdn,
            db_name=self.input.db_name,
            rules_context=self.input.rules_context,
            project_id=self.input.project_id,
            kpi_requirements_block=self.input.kpi_requirements_block,
            reference_model_block=self.input.reference_model_block,
            approved_plan=self.plan,  # Triggers Brain 2 deterministic compilation
            progress_callback=progress_callback,
            verify_loop_enabled=verify,
        )

        return result
