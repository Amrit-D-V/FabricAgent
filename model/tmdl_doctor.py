
from __future__ import annotations

import os
import re
from pathlib import Path

from model.model_quality import find_model_dir


def _parse_column_ref(value: str) -> tuple[str, str]:
    value = value.strip()
    if value.startswith("'"):
        end = value.find("'", 1)
        if end == -1:
            return "", value
        return value[1:end], value[end + 1 :].lstrip(".")
    if "." in value:
        table, col = value.rsplit(".", 1)
        return table, col
    return "", value


def _strip_table_quotes(name: str) -> str:
    name = name.strip()
    if name.startswith("'") and name.endswith("'"):
        return name[1:-1]
    return name


_KEY_SUFFIXES = re.compile(
    r"(key|id|code|num|number|rank|index|seq|count|position|order|version)$", re.IGNORECASE
)
_DOUBLE_SUFFIXES = re.compile(
    r"(amount|price|cost|revenue|sales|margin|rate|pct|percent|discount|tax|total|sum|avg|average|value|budget|target|qty|quantity|weight|height|length|width|size)$",
    re.IGNORECASE,
)
_DATETIME_WORDS = re.compile(r"(date|datetime|time|timestamp)", re.IGNORECASE)
_INT_CALENDAR = re.compile(r"(year|month|quarter|week|day|fiscal)", re.IGNORECASE)
_BOOL_PREFIXES = re.compile(
    r"^(is|has|flag|active|enabled|deleted|visible|archived|approved)", re.IGNORECASE
)


def _infer_datatype(col_name: str) -> str:
    n = col_name.strip("'\" ")
    if _BOOL_PREFIXES.match(n):
        return "boolean"
    if _DATETIME_WORDS.search(n):
        return "dateTime"
    if _KEY_SUFFIXES.search(n):
        return "int64"
    if _INT_CALENDAR.search(n) and not re.search(r"(name|label|desc|key)", n, re.IGNORECASE):
        return "int64"
    if _DOUBLE_SUFFIXES.search(n):
        return "double"
    return "string"


def _load_tmdl_files(definition_dir: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for p in definition_dir.rglob("*.tmdl"):
        rel = p.relative_to(definition_dir).as_posix()
        files[rel] = p.read_text(encoding="utf-8")
    return files


def _write_tmdl_files(definition_dir: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        out = definition_dir / rel.replace("/", os.sep)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")


def _fix_missing_datatypes(
    tmdl_files: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    result = dict(tmdl_files)
    fixed: list[str] = []

    for path, content in tmdl_files.items():
        if not path.startswith("tables/"):
            continue
        lines = content.splitlines()
        new_lines: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            m = re.match(r"^(\t+)(column\s+(.+))$", line)
            if m:
                indent = m.group(1)
                col_name = m.group(3).strip().strip("'\"")
                new_lines.append(line)
                j = i + 1
                block_lines: list[str] = []
                while j < len(lines):
                    nxt = lines[j]
                    if nxt.strip() == "":
                        block_lines.append(nxt)
                        j += 1
                        continue
                    nxt_indent = len(nxt) - len(nxt.lstrip())
                    col_indent = len(indent)
                    if nxt_indent <= col_indent:
                        break
                    block_lines.append(nxt)
                    j += 1
                has_dtype = any("dataType:" in bl for bl in block_lines)
                if not has_dtype:
                    dtype = _infer_datatype(col_name)
                    new_lines.append(f"{indent}\tdataType: {dtype}")
                    fixed.append(
                        f"{path}::{col_name} → {dtype} (guessed from column name, "
                        "not from data — verify before deploying)"
                    )
                new_lines.extend(block_lines)
                i = j
                continue
            new_lines.append(line)
            i += 1
        result[path] = "\n".join(new_lines)

    return result, fixed


def _fix_format_strings(tmdl_files: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    result = dict(tmdl_files)
    fixed: list[str] = []
    bad_pattern = re.compile(r"(formatString:\s*)(C\d\s+[#0,\.]+)")

    for path, content in tmdl_files.items():
        new_content, count = bad_pattern.subn(r'\1"$#,##0.00"', content)
        if count:
            result[path] = new_content
            fixed.append(f"{path}: fixed {count} malformed formatString(s)")

    return result, fixed


def _fix_flat_relationships(
    tmdl_files: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    if "relationships.tmdl" not in tmdl_files:
        return tmdl_files, []

    content = tmdl_files["relationships.tmdl"]
    lines = content.splitlines()
    new_lines: list[str] = []
    fixed: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(
            r"^(relationship)\s+(\w+)\s+(\w+)\s+(\w+)\s+(\w+)\s*$", line.strip()
        )
        if m and ":" not in line:
            from_table, from_col, to_table, to_col = (
                m.group(2),
                m.group(3),
                m.group(4),
                m.group(5),
            )
            name = f"{from_table}_{from_col}_{to_table}_{to_col}"
            new_lines += [
                f"relationship {name}",
                f"\tfromColumn: {from_table}.{from_col}",
                f"\ttoColumn: {to_table}.{to_col}",
                "",
            ]
            fixed.append(name)
            i += 1
            while i < len(lines) and lines[i].startswith("\t"):
                new_lines.append(lines[i])
                i += 1
            continue
        new_lines.append(line)
        i += 1

    result = dict(tmdl_files)
    result["relationships.tmdl"] = "\n".join(new_lines)
    return result, fixed


def _remove_invalid_table_files(
    tmdl_files: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    result = dict(tmdl_files)
    removed: list[str] = []
    _VALID_STARTS = ("table ", "model ", "relationship ")
    for path in list(tmdl_files):
        if not path.startswith("tables/"):
            continue
        content = tmdl_files[path]
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if not lines:
            removed.append(f"{path} (empty file)")
            del result[path]
            continue
        # Verify if any line declares a valid TMDL object
        has_valid_decl = any(
            ln.startswith(kw) for ln in lines for kw in _VALID_STARTS
        )
        if not has_valid_decl:
            removed.append(f"{path} (no table/model/relationship declaration found)")
            del result[path]
    return result, removed


def _remove_embedded_markers(
    tmdl_files: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    result = dict(tmdl_files)
    fixed: list[str] = []
    _MARKER = re.compile(r"=+\s*[Ff][Ii][Ll][Ee]:\s*\S+", re.IGNORECASE)
    for path, content in tmdl_files.items():
        lines = content.splitlines()
        clean = [ln for ln in lines if not _MARKER.search(ln)]
        if len(clean) != len(lines):
            result[path] = "\n".join(clean).strip()
            fixed.append(
                f"{path}: removed {len(lines) - len(clean)} embedded file marker(s)"
            )
    return result, fixed


def _merge_partition_files(
    tmdl_files: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    result = dict(tmdl_files)
    merged: list[str] = []
    for part_path in [
        p for p in tmdl_files if p.startswith("tables/") and p.endswith("_partitions")
    ]:
        base = part_path[len("tables/") : -len("_partitions")]
        parent_path = f"tables/{base}.tmdl"
        partition_content = tmdl_files[part_path].strip()
        if parent_path in result:
            result[parent_path] = (
                result[parent_path].rstrip()
                + "\n\n\t"
                + partition_content.replace("\n", "\n\t")
            )
        else:
            result[parent_path] = (
                f"table {base}\n\n\t{partition_content.replace(chr(10), chr(10) + chr(9))}"
            )
        del result[part_path]
        merged.append(part_path)
    return result, merged


def _remove_orphan_relationships(
    tmdl_files: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    if "relationships.tmdl" not in tmdl_files:
        return tmdl_files, []

    defined_tables = {
        _strip_table_quotes(content.strip().splitlines()[0].replace("table ", ""))
        for path, content in tmdl_files.items()
        if path.startswith("tables/") and content.strip().startswith("table ")
    }

    rel_content = tmdl_files["relationships.tmdl"]
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in rel_content.splitlines():
        if line.startswith("relationship ") and current:
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current and any(ln.strip() for ln in current):
        blocks.append(current)

    ref_pat = re.compile(r"^\s*(?:fromColumn|toColumn):\s*(\S+)\s*$", re.MULTILINE)
    removed: list[str] = []
    kept: list[list[str]] = []
    for block in blocks:
        bt = "\n".join(block)
        used = {
            _parse_column_ref(m.group(1))[0]
            for m in ref_pat.finditer(bt)
            if _parse_column_ref(m.group(1))[0]
        }
        if used and not used.issubset(defined_tables):
            missing = used - defined_tables
            removed.append(f"{block[0].strip()} (missing tables: {', '.join(missing)})")
        else:
            kept.append(block)

    result = dict(tmdl_files)
    if kept:
        result["relationships.tmdl"] = "\n\n".join("\n".join(b) for b in kept)
    elif removed:
        result.pop("relationships.tmdl", None)
    return result, removed


def _fix_placeholder_dax(
    tmdl_files: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    result = dict(tmdl_files)
    fixed: list[str] = []
    for path, content in tmdl_files.items():
        if not path.startswith("tables/"):
            continue
        lines = content.splitlines()
        new_lines: list[str] = []
        for line in lines:
            if "= DAX_EXPRESSION" in line:
                name_m = re.search(r"measure\s+'?([^'=\n]+)'?\s*=", line)
                if name_m:
                    fixed.append(f"{path}::{name_m.group(1).strip()}")
                    line = re.sub(
                        r"= DAX_EXPRESSION.*",
                        "= 0  /* TODO: replace with real DAX */",
                        line,
                    )
            new_lines.append(line)
        result[path] = "\n".join(new_lines)
    return result, fixed


def _deduplicate_columns(
    tmdl_files: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    col_decl_pat = re.compile(r"^\s+column\s+(?:'([^']+)'|(\S+))")
    result = dict(tmdl_files)
    fixed: list[str] = []

    for path, content in tmdl_files.items():
        if not path.startswith("tables/"):
            continue

        lines = content.splitlines()

        # First pass: find every column block (name, [start, end) line range).
        blocks: list[tuple[str, int, int]] = []
        i = 0
        while i < len(lines):
            mc = col_decl_pat.match(lines[i])
            if mc:
                col_name = (mc.group(1) or mc.group(2) or "").strip()
                base_indent = len(lines[i]) - len(lines[i].lstrip())
                start = i
                i += 1
                while i < len(lines):
                    nxt = lines[i]
                    if nxt.strip() == "":
                        i += 1
                        continue
                    if len(nxt) - len(nxt.lstrip()) <= base_indent:
                        break
                    i += 1
                blocks.append((col_name, start, i))
                continue
            i += 1

        by_name: dict[str, list[tuple[int, int]]] = {}
        for name, start, end in blocks:
            by_name.setdefault(name, []).append((start, end))

        # Keep the most complete definition for duplicate columns
        drop_ranges: list[tuple[int, int]] = []
        for name, spans in by_name.items():
            if len(spans) <= 1:
                continue

            def _size(span: tuple[int, int]) -> int:
                s, e = span
                return sum(1 for ln in lines[s:e] if ln.strip())

            spans_sorted = sorted(spans, key=_size, reverse=True)
            for span in spans_sorted[1:]:
                drop_ranges.append(span)
                fixed.append(f"{path}::{name}")

        if drop_ranges:
            drop_set: set[int] = set()
            for s, e in drop_ranges:
                drop_set.update(range(s, e))
            new_lines = [ln for idx, ln in enumerate(lines) if idx not in drop_set]
            result[path] = "\n".join(new_lines)

    return result, fixed


def _validate(tmdl_files: dict[str, str]) -> list[dict]:
    issues: list[dict] = []

    def err(file, msg):
        issues.append({"severity": "error", "file": file, "message": msg})

    def warn(file, msg):
        issues.append({"severity": "warning", "file": file, "message": msg})

    if "model.tmdl" not in tmdl_files:
        err("model.tmdl", "Missing model.tmdl — required root file")

    if "relationships.tmdl" in tmdl_files:
        rel = tmdl_files["relationships.tmdl"]
        for lineno, line in enumerate(rel.splitlines(), 1):
            s = line.strip()
            if s.startswith("relationship ") and ":" not in s and len(s.split()) >= 5:
                err("relationships.tmdl", f"Line {lineno}: flat relationship — '{s}'")
        for block in re.split(r"\nrelationship ", "\n" + rel)[1:]:
            name = block.splitlines()[0].strip()
            if "fromColumn:" not in block:
                err("relationships.tmdl", f"'{name}' missing fromColumn:")
            if "toColumn:" not in block:
                err("relationships.tmdl", f"'{name}' missing toColumn:")

    _MARKER_LINE = re.compile(r"=+\s*[Ff][Ii][Ll][Ee]:\s*\S+", re.IGNORECASE)
    _VALID_TABLE_START = ("table ", "model ", "relationship ")

    table_names: set[str] = set()
    for path, content in tmdl_files.items():
        if not path.startswith("tables/") or not path.endswith(".tmdl"):
            continue
        first = content.strip().splitlines()[0] if content.strip() else ""
        if first.startswith("table "):
            table_names.add(_strip_table_quotes(first[6:]))
        if not any(first.startswith(kw) for kw in _VALID_TABLE_START):
            err(
                path,
                f"File does not start with 'table ' — first line is: '{first[:80]}'",
            )
        for lineno, line in enumerate(content.splitlines(), 1):
            if _MARKER_LINE.search(line):
                err(
                    path,
                    f"Line {lineno}: embedded file marker found inside table file — parser missed a file boundary",
                )
        if "partition " not in content:
            err(path, "No partition block — table has no data source")
        if "partition " in content and "source = ```" not in content:
            warn(path, "Partition present but 'source = ```' block missing")
        for lineno, line in enumerate(content.splitlines(), 1):
            if "= DAX_EXPRESSION" in line:
                nm = re.search(r"measure\s+'?([^'=\n]+)'?\s*=", line)
                err(
                    path,
                    f"Line {lineno}: measure '{(nm.group(1).strip() if nm else '?')}' has placeholder DAX",
                )
        col_lines = content.splitlines()
        for ci, col_line in enumerate(col_lines):
            s = col_line.strip()
            if re.match(r"^column\s+\S+", s) and not s.startswith("//"):
                col_name = re.sub(r"^column\s+", "", s).strip("'")
                found = False
                j = ci + 1
                col_indent = len(col_line) - len(col_line.lstrip())
                while j < len(col_lines):
                    nxt = col_lines[j]
                    if nxt.strip() == "":
                        j += 1
                        continue
                    if len(nxt) - len(nxt.lstrip()) <= col_indent:
                        break
                    if "dataType:" in nxt:
                        found = True
                        break
                    j += 1
                if not found:
                    err(path, f"Column '{col_name}' missing dataType:")
        col_seen: list[str] = []
        for line in content.splitlines():
            mc = re.match(r"^\s+column\s+(?:'([^']+)'|(\S+))", line)
            if mc:
                cn = (mc.group(1) or mc.group(2) or "").strip()
                if cn in col_seen:
                    err(path, f"Column '{cn}' is defined more than once")
                else:
                    col_seen.append(cn)
        if path.endswith("_partitions.tmdl"):
            err(
                path, "Separate _partitions file — must be inline inside the table file"
            )

    if "relationships.tmdl" in tmdl_files:
        for m in re.finditer(
            r"^\s*(?:fromColumn|toColumn):\s*(\S+)\s*$",
            tmdl_files["relationships.tmdl"],
            re.MULTILINE,
        ):
            tbl, _ = _parse_column_ref(m.group(1))
            if tbl and table_names and tbl not in table_names:
                warn(
                    "relationships.tmdl",
                    f"References table '{tbl}' which has no table file",
                )

    return issues


_FIXERS = (
    _remove_invalid_table_files,
    _remove_embedded_markers,
    _merge_partition_files,
    _fix_flat_relationships,
    _remove_orphan_relationships,
    _fix_missing_datatypes,
    _fix_format_strings,
    _fix_placeholder_dax,
    _deduplicate_columns,
)


def diagnose_and_fix(model_path: str, apply: bool = False) -> dict:
    mdir = find_model_dir(model_path)
    if not mdir:
        return {"error": f"no .SemanticModel found from path {model_path!r}"}

    definition_dir = Path(mdir) / "definition"
    if not definition_dir.is_dir():
        return {"error": f"definition/ folder not found in {mdir}"}

    original_files = _load_tmdl_files(definition_dir)
    if not original_files:
        return {"error": f"no .tmdl files found under {definition_dir}"}

    before_issues = _validate(original_files)

    files = dict(original_files)
    fixes_applied: list[str] = []
    for fixer in _FIXERS:
        files, applied = fixer(files)
        fixes_applied.extend(applied)

    after_issues = _validate(files)
    changed = sorted(k for k in files if files[k] != original_files.get(k))

    written = False
    if apply and (changed or set(files) != set(original_files)):
        _write_tmdl_files(definition_dir, files)
        for dp in set(original_files) - set(files):
            (definition_dir / dp.replace("/", os.sep)).unlink(missing_ok=True)
        written = True

    return {
        "model_dir": str(mdir),
        "files_found": len(original_files),
        "issues_before": before_issues,
        "fixes_applied": fixes_applied,
        "issues_after": after_issues,
        "changed_files": changed,
        "written_to_disk": written,
    }
