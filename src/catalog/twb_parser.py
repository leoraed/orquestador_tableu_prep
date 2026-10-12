import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


@dataclass
class TwbDatasource:
    model_name: str
    caption: str
    description: str
    base_table: str
    join_tables: list[str] = field(default_factory=list)
    dimensions: list[dict] = field(default_factory=list)
    metrics: list[dict] = field(default_factory=list)


def _clean(name: str) -> str:
    return name.strip("[]")


def _safe_name(caption: str) -> str:
    s = caption.lower()
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    return s.strip("_") or "modelo"


def parse_twb(content: bytes) -> list[TwbDatasource]:
    """Parse a .twb XML file; returns one TwbDatasource per non-Parameters datasource."""
    root = ET.fromstring(content)
    ds_container = root.find("datasources")
    if ds_container is None:
        raise ValueError("El archivo TWB no contiene datasources.")

    results = []

    for ds in ds_container:
        name = ds.get("name", "")
        caption = ds.get("caption", "") or name

        if name == "Parameters":
            continue

        conn = ds.find("connection")
        tables: list[str] = []
        cols_map: dict[str, str] = {}  # [FieldName] -> table_name

        if conn is not None:
            for rel in conn.findall('.//relation[@type="table"]'):
                t = rel.get("table", "").strip("[]")
                if t and t not in tables:
                    tables.append(t)

            cols_el = conn.find("cols")
            if cols_el is not None:
                for m in cols_el.findall("map"):
                    key = m.get("key", "")
                    val = m.get("value", "")
                    # val format: [table_name].[field_name]
                    if "].[" in val:
                        table_part = val.split("].[")[0].strip("[]")
                        cols_map[key] = table_part

        base_table = tables[0] if tables else ""
        join_tables = tables[1:]

        dimensions: list[dict] = []
        metrics: list[dict] = []
        seen: set[str] = set()

        for col in ds.findall("column"):
            col_name = col.get("name", "")
            col_caption = col.get("caption", "")
            col_role = col.get("role", "")
            col_datatype = col.get("datatype", "")

            if col_name.startswith("[:"):
                continue
            if re.search(r"\((copia|copy)\)_", col_name):
                continue
            if not col_role:
                continue

            display = col_caption if col_caption else _clean(col_name)
            if display in seen:
                continue
            seen.add(display)

            calc_el = col.find("calculation")
            if calc_el is not None:
                expression = calc_el.get("formula", "") or display
            elif col_name in cols_map:
                expression = f"{cols_map[col_name]}.{_clean(col_name)}"
            else:
                expression = _clean(col_name)

            if col_role == "dimension":
                dimensions.append({
                    "name": display,
                    "expression": expression,
                    "data_type": col_datatype,
                    "description": "",
                })
            elif col_role == "measure":
                metrics.append({
                    "name": display,
                    "expression": expression,
                    "format": col.get("default-format", ""),
                    "description": "",
                })

        results.append(TwbDatasource(
            model_name=_safe_name(caption),
            caption=caption,
            description=f"Importado desde Tableau Workbook (fuente: {caption})",
            base_table=base_table,
            join_tables=join_tables,
            dimensions=dimensions,
            metrics=metrics,
        ))

    return results
