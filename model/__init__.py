from __future__ import annotations

# Core model modules
from model.bpa_engine import evaluate as evaluate_bpa, from_reference_model as bpa_meta_from_ref
from model.tmdl_doctor import diagnose_and_fix as diagnose_and_fix_tmdl
from model.dax_analyze import (
    explain_dax,
    optimize_dax,
    debug_dax,
    dax_smells,
)
from model.dax_preflight import extract_references, validate_dax_references
from model.model_docs import generate_docs as generate_model_docs
from model.project_requirements import extract_project_requirements

# Unified pipeline bridge to semantic_model_v2
try:
    from semantic_model_v2.pipeline import (
        generate_model_v2,
        PipelineResult,
        save_pipeline_output,
    )
    from semantic_model_v2.plan import (
        SchemaPlan,
        plan_to_dict,
        dict_to_plan,
        TableSpec,
        ColumnSpec,
        MeasureSpec,
        RelationshipSpec,
    )
    from semantic_model_v2.reference_model_loader import (
        load_pbip_reference,
        ReferenceModel,
    )
    from model.two_brain import (
        SemanticPlanner,
        DeterministicCompiler,
        PlannerInput,
    )
except ImportError:
    generate_model_v2 = None  # type: ignore
    PipelineResult = None  # type: ignore
    save_pipeline_output = None  # type: ignore
    SchemaPlan = None  # type: ignore
    plan_to_dict = None  # type: ignore
    dict_to_plan = None  # type: ignore
    TableSpec = None  # type: ignore
    ColumnSpec = None  # type: ignore
    MeasureSpec = None  # type: ignore
    RelationshipSpec = None  # type: ignore
    load_pbip_reference = None  # type: ignore
    ReferenceModel = None  # type: ignore
    SemanticPlanner = None  # type: ignore
    DeterministicCompiler = None  # type: ignore
    PlannerInput = None  # type: ignore

__all__ = [
    "evaluate_bpa",
    "bpa_meta_from_ref",
    "diagnose_and_fix_tmdl",
    "explain_dax",
    "optimize_dax",
    "debug_dax",
    "dax_smells",
    "extract_references",
    "validate_dax_references",
    "generate_model_docs",
    "extract_project_requirements",
    "generate_model_v2",
    "PipelineResult",
    "save_pipeline_output",
    "SchemaPlan",
    "plan_to_dict",
    "dict_to_plan",
    "TableSpec",
    "ColumnSpec",
    "MeasureSpec",
    "RelationshipSpec",
    "load_pbip_reference",
    "ReferenceModel",
    "SemanticPlanner",
    "DeterministicCompiler",
    "PlannerInput",
]

