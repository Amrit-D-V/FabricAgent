from __future__ import annotations

import re


def _san_table(name: str) -> str:
    name = (name or "Data").strip() or "Data"
    return name


def _needs_quote(name: str) -> bool:
    return bool(re.search(r"[^A-Za-z0-9_]", name))


def _q(name: str) -> str:
    return f"'{name}'" if _needs_quote(name) else name


def infer_tmdl_type(dtype_kind: str) -> tuple[str, str]:
    return {
        "i": ("int64", "Int64.Type"),
        "u": ("int64", "Int64.Type"),
        "f": ("double", "type number"),
        "b": ("boolean", "type logical"),
        "M": ("dateTime", "type datetime"),
    }.get(dtype_kind, ("string", "type text"))


def infer_columns(df) -> list[tuple[str, str, str]]:
    cols = []
    for col in df.columns:
        kind = getattr(df[col].dtype, "kind", "O")
        tmdl_t, pq_t = infer_tmdl_type(kind)
        cols.append((str(col), tmdl_t, pq_t))
    return cols


def _partition_m(
    table: str, columns: list[tuple[str, str, str]], source_path: str, is_excel: bool
) -> str:
    safe_path = source_path.replace("\\", "\\\\").replace('"', '\\"')
    transforms = ", ".join(f'{{"{c}", {pq}}}' for c, _, pq in columns)
    if is_excel:
        load = (
            f'    Source = Excel.Workbook(File.Contents("{safe_path}"), null, true),\n'
            f'    Sheet = Source{{[Kind="Sheet"]}}[Data],\n'
            f"    Promoted = Table.PromoteHeaders(Sheet, [PromoteAllScalars=true])"
        )
    else:
        load = (
            f'    Source = Csv.Document(File.Contents("{safe_path}"), '
            f'[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n'
            f"    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true])"
        )
    typed = (
        f"    Typed = Table.TransformColumnTypes(Promoted, {{{transforms}}})"
        if transforms
        else "    Typed = Promoted"
    )
    return (
        "```\n"
        "\t\t\tlet\n"
        f"\t\t\t{load}\n,\n"
        f"\t\t\t{typed}\n"
        "\t\t\tin\n"
        "\t\t\t    Typed\n"
        "\t\t\t```"
    )


def dataframe_to_tmdl(
    df,
    table_name: str,
    model_name: str = "",
    source_path: str = "",
    is_excel: bool = False,
) -> dict[str, str]:
    table = _san_table(table_name)
    columns = infer_columns(df)

    model_tmdl = (
        "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
    )

    lines = [f"table {_q(table)}", ""]
    for col, tmdl_t, _ in columns:
        lines.append(f"\tcolumn {_q(col)}")
        lines.append(f"\t\tdataType: {tmdl_t}")
        lines.append(f"\t\tsourceColumn: {col}")
        lines.append("")
    lines.append(f"\tpartition {_q(table)} = m")
    lines.append("\t\tmode: import")
    lines.append("\t\tsource = " + _partition_m(table, columns, source_path, is_excel))
    lines.append("")

    return {
        "model.tmdl": model_tmdl,
        f"tables/{table}.tmdl": "\n".join(lines),
    }
