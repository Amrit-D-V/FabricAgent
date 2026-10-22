import json
import os
import re
import struct
import time
from dataclasses import dataclass
from dataclasses import field as dc_field

from db import database as db
from util.logger_setup import get_logger

logger = get_logger(__name__)


AGG_FUNC_NAMES = {0: "SUM", 1: "AVERAGE", 2: "MIN", 3: "MAX", 4: "COUNT", 5: "COUNTA"}
SKIP_VISUAL_TYPES = {
    "slicer",
    "textbox",
    "shape",
    "image",
    "actionButton",
    "list_slicer",
}


@dataclass
class FieldBinding:
    entity: str
    property: str
    query_ref: str
    native_ref: str
    is_measure: bool
    role: str
    agg_func: str | None
    field_type: str


@dataclass
class VisualBinding:
    visual_name: str
    visual_type: str
    title: str
    page_name: str
    page_display_name: str
    fields: list = dc_field(default_factory=list)
    measures: list = dc_field(default_factory=list)
    dimensions: list = dc_field(default_factory=list)
    visual_json_path: str = ""


@dataclass
class FixSuggestion:
    visual_name: str
    explanation: str
    fix_description: str
    visual_json_path: str
    current_json: dict
    proposed_json: dict
    error: str | None = None


@dataclass
class QueryPair:
    visual_name: str
    dax_query: str
    sql_query: str
    notes: str = ""
    generation_error: str | None = None


@dataclass
class ExecutionResult:
    rows: list
    row_count: int
    error: str | None = None
    raw_response: dict | None = None


def _exec_error(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


@dataclass
class ComparisonResult:
    status: str
    dax_row_count: int
    sql_row_count: int
    numeric_delta: float | None = None
    mismatched_rows: list = dc_field(default_factory=list)
    notes: str = ""


def _parse_single_field(
    field: dict, query_ref: str, native_ref: str, role: str
) -> FieldBinding | None:
    if "Measure" in field:
        m = field["Measure"]
        entity = m.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
        prop = m.get("Property", "")
        return FieldBinding(
            entity=entity,
            property=prop,
            query_ref=query_ref,
            native_ref=native_ref,
            is_measure=True,
            role=role,
            agg_func=None,
            field_type="Measure",
        )

    if "Aggregation" in field:
        agg = field["Aggregation"]
        col = agg.get("Expression", {}).get("Column", {})
        entity = col.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
        prop = col.get("Property", "")
        func_code = agg.get("Function", 0)
        return FieldBinding(
            entity=entity,
            property=prop,
            query_ref=query_ref,
            native_ref=native_ref,
            is_measure=False,
            role=role,
            agg_func=AGG_FUNC_NAMES.get(func_code, "SUM"),
            field_type="Aggregation",
        )

    if "Column" in field:
        col = field["Column"]
        entity = col.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
        prop = col.get("Property", "")
        return FieldBinding(
            entity=entity,
            property=prop,
            query_ref=query_ref,
            native_ref=native_ref,
            is_measure=False,
            role=role,
            agg_func=None,
            field_type="Column",
        )

    return None


def parse_report_visuals(report_path: str) -> list[VisualBinding]:
    report_def_dir = None
    for d in os.listdir(report_path):
        if d.endswith(".Report"):
            candidate = os.path.join(report_path, d, "definition")
            if os.path.isdir(candidate):
                report_def_dir = candidate
                break

    if not report_def_dir:
        raise FileNotFoundError(
            f"No .Report/definition directory found in {report_path}"
        )

    pages_dir = os.path.join(report_def_dir, "pages")
    if not os.path.isdir(pages_dir):
        raise FileNotFoundError(f"No pages directory in {report_def_dir}")

    pages_json = os.path.join(pages_dir, "pages.json")
    page_order = []
    if os.path.exists(pages_json):
        with open(pages_json) as f:
            page_order = json.load(f).get("pageOrder", [])
    if not page_order:
        page_order = sorted(
            d
            for d in os.listdir(pages_dir)
            if os.path.isdir(os.path.join(pages_dir, d))
        )

    visuals: list[VisualBinding] = []
    for page_name in page_order:
        page_dir = os.path.join(pages_dir, page_name)
        if not os.path.isdir(page_dir):
            continue

        page_display_name = page_name
        page_json_path = os.path.join(page_dir, "page.json")
        if os.path.exists(page_json_path):
            with open(page_json_path) as f:
                page_display_name = json.load(f).get("displayName", page_name)

        visuals_dir = os.path.join(page_dir, "visuals")
        if not os.path.isdir(visuals_dir):
            continue

        for visual_name in os.listdir(visuals_dir):
            visual_json_path = os.path.join(visuals_dir, visual_name, "visual.json")
            if not os.path.exists(visual_json_path):
                continue

            with open(visual_json_path) as f:
                vjson = json.load(f)

            v_data = vjson.get("visual", {})
            visual_type = v_data.get("visualType", "unknown")

            if visual_type in SKIP_VISUAL_TYPES:
                continue

            title = visual_name
            try:
                title_val = (
                    v_data.get("visualContainerObjects", {})
                    .get("title", [{}])[0]
                    .get("properties", {})
                    .get("text", {})
                    .get("expr", {})
                    .get("Literal", {})
                    .get("Value", "")
                )
                if title_val:
                    title = title_val.strip("'\"")
            except (AttributeError, IndexError, KeyError, TypeError):
                # Only the shapes a hand-edited visual.json can take. Anything
                # else here is a bug in the walk above, not a titleless visual.
                logger.debug(
                    f"Could not read title for visual {visual_name}, "
                    f"falling back to the folder name",
                    exc_info=True,
                )

            query_state = v_data.get("query", {}).get("queryState", {})
            fields: list[FieldBinding] = []
            for role, role_data in query_state.items():
                for proj in role_data.get("projections", []):
                    binding = _parse_single_field(
                        proj.get("field", {}),
                        proj.get("queryRef", ""),
                        proj.get("nativeQueryRef", ""),
                        role,
                    )
                    if binding:
                        fields.append(binding)

            if not fields:
                continue

            visuals.append(
                VisualBinding(
                    visual_name=visual_name,
                    visual_type=visual_type,
                    title=title,
                    page_name=page_name,
                    page_display_name=page_display_name,
                    fields=fields,
                    measures=[f for f in fields if f.field_type == "Measure"],
                    dimensions=[f for f in fields if f.field_type == "Column"],
                    visual_json_path=visual_json_path,
                )
            )

    return visuals


def extract_dataset_id(report_path: str) -> str:
    for item in os.listdir(report_path):
        if item.endswith(".Report"):
            report_dir = os.path.join(report_path, item)

            model_file = os.path.join(report_dir, "definition", "model.json")
            if os.path.exists(model_file):
                try:
                    with open(model_file) as f:
                        model = json.load(f)
                    if model.get("dataModelName"):
                        pass
                except Exception:
                    logger.debug(f"Could not read {model_file}", exc_info=True)

            for fname in ["connections.json", "connections", "manifest.json"]:
                fpath = os.path.join(report_dir, fname)
                if os.path.exists(fpath):
                    try:
                        with open(fpath) as f:
                            content = json.load(f)
                        for val in content.values():
                            if isinstance(val, dict):
                                for v in val.values():
                                    if (
                                        isinstance(v, str)
                                        and len(v) == 36
                                        and v.count("-") == 4
                                    ):
                                        return v
                    except Exception:
                        logger.debug(
                            f"Could not scan {fpath} for a dataset id", exc_info=True
                        )

            visuals_dir = os.path.join(report_dir, "definition", "pages")
            if os.path.exists(visuals_dir):
                for page_dir in os.listdir(visuals_dir):
                    visuals_path = os.path.join(visuals_dir, page_dir, "visuals")
                    if os.path.isdir(visuals_path):
                        for visual_dir in os.listdir(visuals_path):
                            visual_file = os.path.join(
                                visuals_path, visual_dir, "visual.json"
                            )
                            if os.path.exists(visual_file):
                                try:
                                    with open(visual_file) as f:
                                        visual = json.load(f)
                                    if "datasetReference" in visual:
                                        ref = visual["datasetReference"]
                                        if isinstance(ref, dict) and "id" in ref:
                                            return ref["id"]
                                    visual_str = json.dumps(visual)
                                    m = re.search(
                                        r'"id"\s*:\s*"([a-f0-9\-]{36})"', visual_str
                                    )
                                    if m:
                                        return m.group(1)
                                except Exception:
                                    logger.debug(
                                        f"Could not scan {visual_file} for a "
                                        f"dataset id",
                                        exc_info=True,
                                    )

    return ""


def _extract_relevant_tmdl(tmdl_files: dict, entities: set) -> str:
    norm_ents = {e.lower().replace(" ", "").replace("_", "") for e in entities}
    parts = []
    for path, content in tmdl_files.items():
        if "relationship" in path.lower():
            parts.append(f"--- {path} ---\n{content}")
            continue
        table_key = (
            os.path.basename(path)
            .replace(".tmdl", "")
            .lower()
            .replace(" ", "")
            .replace("_", "")
        )
        if any(table_key in e or e in table_key for e in norm_ents):
            parts.append(f"--- {path} ---\n{content}")
    if not parts:
        all_items = list(tmdl_files.items())
        parts = [f"--- {p} ---\n{c}" for p, c in all_items[:8]]
    return "\n\n".join(parts)


BATCH_SIZE = 5


def _generate_queries_for_batch(
    batch: list[VisualBinding],
    tmdl_context: str,
    server_fqdn: str,
    db_name: str,
    client,
    project_id: int | None,
) -> list[QueryPair]:
    visual_descs = []
    for v in batch:
        visual_descs.append(
            {
                "visual_name": v.visual_name,
                "visual_type": v.visual_type,
                "title": v.title,
                "page": v.page_display_name,
                "named_measures": [
                    {
                        "entity": f.entity,
                        "property": f.property,
                        "query_ref": f.query_ref,
                    }
                    for f in v.fields
                    if f.field_type == "Measure"
                ],
                "column_aggregations": [
                    {
                        "entity": f.entity,
                        "property": f.property,
                        "agg_func": f.agg_func,
                        "query_ref": f.query_ref,
                        "role": f.role,
                    }
                    for f in v.fields
                    if f.field_type == "Aggregation"
                ],
                "dimension_columns": [
                    {"entity": f.entity, "property": f.property, "role": f.role}
                    for f in v.fields
                    if f.field_type == "Column"
                ],
            }
        )

    prompt = f"""You are a Power BI and SQL expert. Generate validation query pairs for these dashboard visuals.

SOURCE DATABASE: server={server_fqdn or "see TMDL"}, database={db_name or "see TMDL"}

SEMANTIC MODEL (TMDL):
{tmdl_context[:6000]}

VISUALS TO VALIDATE ({len(batch)} visuals):
{json.dumps(visual_descs, indent=2)}

For each visual, generate:

1. DAX query (EVALUATE block):
   - Card/KPI with named measure: EVALUATE {{ [MeasureName] }}
   - Card/KPI with column aggregation: EVALUATE {{ CALCULATE(AGG_FUNC(Table[Column])) }}
   - Chart/matrix with dimension + measure: EVALUATE SUMMARIZECOLUMNS(Table[DimCol], "Value", [Measure])
   - Chart/matrix with dimension + aggregation: EVALUATE SUMMARIZECOLUMNS(Table[DimCol], "Value", CALCULATE(AGG_FUNC(Table[ValueCol])))

2. SQL query:
   - Use source table/column names from TMDL M expressions (look for Source= steps)
   - Match the same aggregation (SUM/AVG/MIN/MAX/COUNT)
   - GROUP BY dimension columns for charts
   - Keep entire SQL on ONE line — use space instead of newlines between clauses
   - If M expression not available, use entity/column names as-is with a note

Return ONLY a valid JSON array — one object per visual, same order as input.
NO markdown code fences. NO explanation text. Just the raw array:

[
  {{
    "visual_name": "...",
    "dax_query": "EVALUATE ...",
    "sql_query": "SELECT ... FROM ... WHERE ...",
    "notes": "any assumptions"
  }}
]"""

    try:
        start_time = time.time()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = (time.time() - start_time) * 1000

        logger.info(
            f"Validation batch ({len(batch)} visuals): {response.usage.input_tokens}→{response.usage.output_tokens} tokens",
            extra={
                "agent": "validation",
                "model": "claude-sonnet-4-6",
                "tokens_in": response.usage.input_tokens,
                "tokens_out": response.usage.output_tokens,
                "latency_ms": int(latency_ms),
            },
        )

        if project_id:
            db.save_api_usage(
                project_id=project_id,
                agent="validation",
                model="claude-sonnet-4-6",
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                latency_ms=int(latency_ms),
                status="success",
            )

        raw = response.content[0].text.strip()
        logger.debug(f"Batch response (first 200 chars): {raw[:200]}")

        cleaned = raw
        cleaned = cleaned.removeprefix("```json")
        cleaned = cleaned.removeprefix("```")
        cleaned = cleaned.strip()

        first_bracket = cleaned.find("[")
        last_bracket = cleaned.rfind("]")

        if first_bracket == -1 or last_bracket == -1 or last_bracket <= first_bracket:
            logger.error(f"No JSON array in batch response: {cleaned[:200]}")
            raise ValueError(f"No JSON array found. Response: {cleaned[:150]}...")

        json_raw = cleaned[first_bracket : last_bracket + 1]
        logger.debug(f"Extracted JSON (length {len(json_raw)})")

        try:
            pairs_raw = json.loads(json_raw)
        except json.JSONDecodeError as je:
            logger.debug(f"Parse error at pos {je.pos}, trying newline fix...")
            import re

            fixed = re.sub(
                r'(?<=":\ ")(.*?)(?=")',
                lambda m: m.group(0).replace("\n", "\\n"),
                json_raw,
                flags=re.DOTALL,
            )
            pairs_raw = json.loads(fixed)
            logger.info("Batch JSON parsed after newline fix")

        return [
            QueryPair(
                visual_name=p.get(
                    "visual_name", batch[i].visual_name if i < len(batch) else ""
                ),
                dax_query=p.get("dax_query", ""),
                sql_query=p.get("sql_query", ""),
                notes=p.get("notes", ""),
            )
            for i, p in enumerate(pairs_raw)
        ]

    except json.JSONDecodeError as je:
        logger.error(f"JSON parse error for batch: {je}")
        return [
            QueryPair(
                visual_name=v.visual_name,
                dax_query="",
                sql_query="",
                generation_error=f"JSON error: {je}",
            )
            for v in batch
        ]
    except Exception as e:
        logger.error(f"Batch query generation failed: {e}")
        return [
            QueryPair(
                visual_name=v.visual_name,
                dax_query="",
                sql_query="",
                generation_error=str(e),
            )
            for v in batch
        ]


def generate_validation_queries_batch(
    visuals: list[VisualBinding],
    tmdl_files: dict,
    server_fqdn: str = "",
    db_name: str = "",
    project_id: int | None = None,
) -> list[QueryPair]:
    import anthropic
    from dotenv import load_dotenv

    load_dotenv()
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    all_entities = {f.entity for v in visuals for f in v.fields}
    tmdl_context = _extract_relevant_tmdl(tmdl_files, all_entities)

    results: list[QueryPair] = []
    batches = [visuals[i : i + BATCH_SIZE] for i in range(0, len(visuals), BATCH_SIZE)]
    logger.info(
        f"Processing {len(visuals)} visuals in {len(batches)} batch(es) of up to {BATCH_SIZE}"
    )

    for batch_num, batch in enumerate(batches, 1):
        logger.info(
            f"Generating queries for batch {batch_num}/{len(batches)} ({len(batch)} visuals)"
        )
        batch_results = _generate_queries_for_batch(
            batch, tmdl_context, server_fqdn, db_name, client, project_id
        )
        results.extend(batch_results)

    return results


_DSCMD_SEARCH_PATHS = [
    r"C:\Program Files\DAX Studio\dscmd.exe",
    r"C:\Program Files (x86)\DAX Studio\dscmd.exe",
    os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Programs", "DAX Studio", "dscmd.exe"
    ),
]


def find_dscmd() -> str | None:
    for p in _DSCMD_SEARCH_PATHS:
        if os.path.isfile(p):
            return p
    return None


def execute_dax_dscmd(
    dax_query: str, workspace_name: str, model_name: str
) -> ExecutionResult:
    import csv
    import subprocess
    import tempfile

    dscmd = find_dscmd()
    if not dscmd:
        return ExecutionResult(
            rows=[],
            row_count=0,
            error="dscmd.exe not found — install DAX Studio to use this path",
        )

    server = f"powerbi://api.powerbi.com/v1.0/myorg/{workspace_name}"

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        out_path = tmp.name

    try:
        proc = subprocess.run(
            [
                dscmd,
                "CSV",
                out_path,
                "--server",
                server,
                "--database",
                model_name,
                "--query",
                dax_query,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            return ExecutionResult(
                rows=[], row_count=0, error=(proc.stderr or proc.stdout).strip()
            )

        rows: list[dict] = []
        with open(out_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                rows.append(dict(row))
        return ExecutionResult(rows=rows, row_count=len(rows))
    except Exception as exc:
        logger.exception(f"dscmd DAX execution failed: {exc}")
        return ExecutionResult(rows=[], row_count=0, error=_exec_error(exc))
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            logger.debug(f"Could not remove temp CSV {out_path}", exc_info=True)


def execute_dax(dataset_id: str, workspace_id: str, dax_query: str) -> ExecutionResult:
    import requests

    from auth.token_manager import get_token_cached

    url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
    try:
        resp = requests.post(
            url,
            json={
                "queries": [{"query": dax_query}],
                "serializerSettings": {"includeNulls": True},
            },
            headers={
                "Authorization": f"Bearer {get_token_cached()}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("results", [{}])[0].get("tables", [{}])[0].get("rows", [])
        return ExecutionResult(rows=rows, row_count=len(rows), raw_response=data)
    except Exception as e:
        logger.exception(f"DAX execution failed for dataset {dataset_id}: {e}")
        return ExecutionResult(rows=[], row_count=0, error=_exec_error(e))


def execute_sql(server_fqdn: str, db_name: str, sql_query: str) -> ExecutionResult:
    import pyodbc

    from auth.token_manager import get_sql_token

    try:
        token = get_sql_token()
        token_bytes = token.encode("utf-16-le")
        token_struct = struct.pack(
            f"<I{len(token_bytes)}s", len(token_bytes), token_bytes
        )
        conn = pyodbc.connect(
            "DRIVER={ODBC Driver 18 for SQL Server};"
            f"SERVER={server_fqdn};DATABASE={db_name};"
            "Encrypt=yes;TrustServerCertificate=no;",
            attrs_before={1256: token_struct},
            timeout=30,
        )
        cursor = conn.cursor()
        cursor.execute(sql_query)
        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        conn.close()
        return ExecutionResult(rows=rows, row_count=len(rows))
    except Exception as e:
        logger.exception(f"SQL execution failed on {server_fqdn}/{db_name}: {e}")
        return ExecutionResult(rows=[], row_count=0, error=_exec_error(e))


def compare_results(
    dax_result: ExecutionResult,
    sql_result: ExecutionResult,
    tolerance: float = 0.0001,
) -> ComparisonResult:
    if dax_result.error:
        return ComparisonResult(
            status="error",
            dax_row_count=0,
            sql_row_count=0,
            notes=f"DAX error: {dax_result.error}",
        )
    if sql_result.error:
        return ComparisonResult(
            status="error",
            dax_row_count=dax_result.row_count,
            sql_row_count=0,
            notes=f"SQL error: {sql_result.error}",
        )

    dax_rows = dax_result.rows
    sql_rows = sql_result.rows

    if len(dax_rows) != len(sql_rows):
        return ComparisonResult(
            status="mismatch",
            dax_row_count=len(dax_rows),
            sql_row_count=len(sql_rows),
            notes=f"Row count mismatch: DAX={len(dax_rows)}, SQL={len(sql_rows)}",
        )

    if not dax_rows:
        return ComparisonResult(
            status="match",
            dax_row_count=0,
            sql_row_count=0,
            numeric_delta=0.0,
            notes="Both returned 0 rows",
        )

    def norm(row: dict) -> dict:
        return {re.sub(r"[\[\]]", "", k).strip().lower(): v for k, v in row.items()}

    dax_n = sorted(
        [norm(r) for r in dax_rows], key=lambda r: str(next(iter(r.values()), ""))
    )
    sql_n = sorted(
        [norm(r) for r in sql_rows], key=lambda r: str(next(iter(r.values()), ""))
    )

    max_delta = 0.0
    mismatched: list[dict] = []

    for i, (d_row, s_row) in enumerate(zip(dax_n, sql_n)):
        for col in d_row:
            if col not in s_row:
                continue
            d_val, s_val = d_row[col], s_row[col]
            try:
                d_num, s_num = float(d_val), float(s_val)
                delta = abs(d_num - s_num) / max(abs(d_num), 1.0)
                max_delta = max(max_delta, delta)
                if delta > tolerance:
                    mismatched.append(
                        {
                            "row": i,
                            "col": col,
                            "dax": d_num,
                            "sql": s_num,
                            "delta_pct": round(delta * 100, 4),
                        }
                    )
            except (TypeError, ValueError):
                if str(d_val) != str(s_val):
                    mismatched.append(
                        {
                            "row": i,
                            "col": col,
                            "dax": d_val,
                            "sql": s_val,
                            "delta_pct": None,
                        }
                    )

    if mismatched:
        return ComparisonResult(
            status="mismatch",
            dax_row_count=len(dax_rows),
            sql_row_count=len(sql_rows),
            numeric_delta=max_delta,
            mismatched_rows=mismatched[:10],
            notes=f"{len(mismatched)} value(s) outside {tolerance * 100:.3f}% tolerance",
        )

    return ComparisonResult(
        status="match",
        dax_row_count=len(dax_rows),
        sql_row_count=len(sql_rows),
        numeric_delta=max_delta,
        notes="All values within tolerance",
    )


def generate_visual_fix(
    visual: VisualBinding,
    comparison: ComparisonResult,
    pair: QueryPair,
    tmdl_files: dict,
) -> FixSuggestion:
    import anthropic
    from dotenv import load_dotenv

    load_dotenv()

    if not visual.visual_json_path or not os.path.exists(visual.visual_json_path):
        return FixSuggestion(
            visual_name=visual.visual_name,
            explanation="",
            fix_description="",
            visual_json_path=visual.visual_json_path or "",
            current_json={},
            proposed_json={},
            error=f"visual.json not found at: {visual.visual_json_path}",
        )

    with open(visual.visual_json_path, encoding="utf-8") as f:
        current_json = json.load(f)

    entities = {f.entity for f in visual.fields}
    tmdl_context = _extract_relevant_tmdl(tmdl_files, entities)

    mismatch_detail = ""
    if comparison:
        mismatch_detail = f"Status: {comparison.status}\nDAX rows: {comparison.dax_row_count}, SQL rows: {comparison.sql_row_count}"
        if comparison.mismatched_rows:
            mismatch_detail += f"\nMismatched values (first 5):\n{json.dumps(comparison.mismatched_rows[:5], indent=2)}"
        if comparison.notes:
            mismatch_detail += f"\nNotes: {comparison.notes}"

    prompt = f"""You are a Power BI report expert. A dashboard visual has failed validation.
Your job is to propose the MINIMAL edit to the visual.json to fix the mismatch.
You CANNOT change the Fabric semantic model — only the visual.json file.

VISUAL INFO:
- Name: {visual.visual_name}
- Type: {visual.visual_type}
- Title: {visual.title}
- Page: {visual.page_display_name}
- Current fields: {json.dumps([{"entity": f.entity, "property": f.property, "role": f.role, "type": f.field_type, "agg": f.agg_func} for f in visual.fields], indent=2)}

VALIDATION RESULT:
{mismatch_detail}

DAX QUERY USED: {pair.dax_query}
SQL QUERY USED: {pair.sql_query}

SEMANTIC MODEL (TMDL — available measures/columns):
{tmdl_context[:4000]}

CURRENT visual.json:
{json.dumps(current_json, indent=2)[:3000]}

Analyze why the visual shows wrong data and propose a fix.
Fixes allowed (dashboard layer only):
- Change which measure/column is bound to a role (edit queryState projections)
- Change implicit aggregation function (SUM/COUNT/AVERAGE/MIN/MAX)
- Add or remove a visual-level filter in visual.json
- Change the visual type (visualType field)

Return ONLY valid JSON with this structure (no markdown, no extra text):
{{
  "explanation": "one sentence: why the data is wrong",
  "fix_description": "one sentence: what this change does",
  "proposed_json": {{ ...the complete corrected visual.json content... }}
}}"""

    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        raw = raw.removeprefix("```json")
        raw = raw.removeprefix("```")
        raw = raw.strip().rstrip("`").strip()

        result = json.loads(raw)
        return FixSuggestion(
            visual_name=visual.visual_name,
            explanation=result.get("explanation", ""),
            fix_description=result.get("fix_description", ""),
            visual_json_path=visual.visual_json_path,
            current_json=current_json,
            proposed_json=result.get("proposed_json", current_json),
        )
    except Exception as e:
        logger.error(f"Fix generation failed for {visual.visual_name}: {e}")
        return FixSuggestion(
            visual_name=visual.visual_name,
            explanation="",
            fix_description="",
            visual_json_path=visual.visual_json_path,
            current_json=current_json,
            proposed_json=current_json,
            error=str(e),
        )
