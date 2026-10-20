import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field

from src.catalog.tableau_to_sql import NoTraducible, make_resolver, translate_formula


@dataclass
class TwbDatasource:
    model_name: str
    caption: str
    description: str
    base_table: str
    join_tables: list[str] = field(default_factory=list)
    dimensions: list[dict] = field(default_factory=list)
    metrics: list[dict] = field(default_factory=list)
    # Campos que quedaron con la fórmula Tableau original: {name, kind, reason}
    pendientes: list[dict] = field(default_factory=list)
    # Problemas del datasource completo (ej. fuente publicada sin tabla real)
    avisos: list[str] = field(default_factory=list)
    # Origen como consulta SQL personalizada (FROM (base_sql) AS base_table)
    base_sql: str | None = None
    # Joins con su condición (solo fuentes con modelo de relaciones; ver twb_relaciones)
    joins: list[dict] = field(default_factory=list)
    # Modelo de relaciones: fuente de origen y sus tablas lógicas (cuáles son hechos)
    grupo: str = ""
    tablas: list[dict] = field(default_factory=list)


# Conexión a una fuente publicada en Tableau Server: no es una tabla del DW
_TABLAS_PROXY = {"sqlproxy"}


def _clean(name: str) -> str:
    return name.strip("[]")


_REF_RE = re.compile(r"\[(?:[^\]]|\]\])*\](?:\.\[(?:[^\]]|\]\])*\])?")


def _formula_legible(formula: str, defs: dict) -> str:
    """Reemplaza nombres internos ([Calculation_123]) por su caption ([cond_OrbithHome])."""
    def repl(m: re.Match) -> str:
        ref = m.group()
        info = defs.get(ref)
        if info and info["caption"] != _clean(ref):
            return "[" + info["caption"].replace("]", "]]") + "]"
        return ref
    return _REF_RE.sub(repl, formula)


def _safe_name(caption: str) -> str:
    s = caption.lower()
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    return s.strip("_") or "modelo"


def parse_twb(content: bytes, hechos: dict[str, list[str]] | None = None) -> list[TwbDatasource]:
    """
    Parse a .twb XML file.
    Only imports fields actually used in worksheets (avoids unused and internal copies).
    Falls back to all non-copy fields if the file has no worksheets.

    Las fuentes con modelo de relaciones se importan como un modelo por tabla de hechos
    (twb_relaciones). hechos: {caption de la fuente: [tablas lógicas]} para elegirlas a mano.
    """
    from src.catalog import twb_relaciones
    root = ET.fromstring(content)
    ds_container = root.find("datasources")
    if ds_container is None:
        raise ValueError("El archivo TWB no contiene datasources.")

    # ── Step 1: build full column index per datasource ──────────────────────
    col_defs: dict[str, dict] = {}      # ds_name -> {col_name: info_dict}
    col_table: dict[str, dict] = {}     # ds_name -> {col_name: (table, remote_col)}
    ds_tables: dict[str, list] = {}     # ds_name -> [table_names]
    ds_captions: dict[str, str] = {}    # ds_name -> human caption
    ds_proxy: set[str] = set()          # datasources que son fuentes publicadas
    ds_sql: dict[str, list] = {}        # ds_name -> [(relation, sql)] consultas personalizadas
    ds_elem: dict[str, object] = {}

    for ds in ds_container:
        ds_name = ds.get("name", "")
        caption = ds.get("caption", "") or ds_name
        if ds_name == "Parameters":
            continue

        ds_captions[ds_name] = caption
        col_defs[ds_name] = {}
        col_table[ds_name] = {}
        ds_tables[ds_name] = []
        ds_sql[ds_name] = []
        ds_elem[ds_name] = ds

        conn = ds.find("connection")
        if conn is not None:
            for rel in conn.findall('.//relation[@type="text"]'):
                if rel.get("name") and all(n != rel.get("name") for n, _ in ds_sql[ds_name]):
                    ds_sql[ds_name].append((rel.get("name"), twb_relaciones.desescapar_sql(rel.text or "")))
            for rel in conn.findall('.//relation[@type="table"]'):
                t = rel.get("table", "").strip("[]")
                if t.lower() in _TABLAS_PROXY:
                    ds_proxy.add(ds_name)
                elif t and t not in ds_tables[ds_name]:
                    ds_tables[ds_name].append(t)

            cols_el = conn.find("cols")
            if cols_el is not None:
                for m in cols_el.findall("map"):
                    key = m.get("key", "")
                    val = m.get("value", "")
                    if "].[" in val:
                        table, remote = val.split("].[", 1)
                        table = table.strip("[]")
                        col_table[ds_name][key] = (
                            None if table.lower() in _TABLAS_PROXY else table, remote.strip("[]"))

        for col in ds.findall("column"):
            name = col.get("name", "")
            if not name:
                continue
            calc = col.find("calculation")
            col_defs[ds_name][name] = {
                "caption": col.get("caption", "") or _clean(name),
                "role": col.get("role", ""),
                "datatype": col.get("datatype", "string"),
                "formula": calc.get("formula", "") if calc is not None else "",
                "fmt": col.get("default-format", ""),
                "agg": col.get("aggregation", ""),
            }

    # ── Step 2: collect used fields per datasource from worksheets ───────────
    used: dict[str, set] = defaultdict(set)   # ds_name -> set of col names
    deps: dict[str, dict] = defaultdict(dict)  # ds_name -> {col: atributos en las hojas}
    worksheets = root.find("worksheets")
    if worksheets is not None:
        for ws in worksheets:
            table_el = ws.find("table")
            if table_el is None:
                continue
            view = table_el.find("view")
            if view is None:
                continue
            for dd in view.findall("datasource-dependencies"):
                ds_name = dd.get("datasource", "")
                if ds_name not in col_defs:
                    continue
                for col in dd.findall("column"):
                    fn = col.get("name", "")
                    if fn:
                        used[ds_name].add(fn)
                        deps[ds_name].setdefault(fn, dict(col.attrib))

    # ── Step 3: build one TwbDatasource per datasource ───────────────────────
    results = []

    for ds_name, caption in ds_captions.items():
        if twb_relaciones.es_modelo_de_relaciones(ds_elem[ds_name]):
            for d in twb_relaciones.importar(ds_elem[ds_name], col_defs[ds_name], deps[ds_name], used.get(ds_name),
                                             set(ds_captions), (hechos or {}).get(caption)):
                results.append(TwbDatasource(**d))
            continue

        defs = col_defs[ds_name]
        ctmap = col_table[ds_name]
        tables = ds_tables[ds_name]
        # Sin tablas físicas: la primera consulta personalizada es el origen (FROM (sql) AS `nombre`)
        base_sql = None
        if not tables and ds_sql[ds_name]:
            nombre_sql, base_sql = ds_sql[ds_name][0]
            tables = [nombre_sql]

        # Use worksheet-filtered set; fall back to all columns if no worksheets
        candidate_names = used.get(ds_name) or set(defs.keys())

        # Referencias se resuelven contra TODAS las columnas del datasource,
        # no solo las usadas en hojas (los cálculos intermedios no se importan).
        resolve = make_resolver(
            defs, ctmap, tables[0] if tables else "", set(ds_captions), ds_name
        )

        dimensions: list[dict] = []
        metrics: list[dict] = []
        pendientes: list[dict] = []
        seen_captions: set[str] = set()

        for fn in candidate_names:
            info = defs.get(fn)
            if not info:
                continue

            role = info["role"]
            if not role:
                continue

            display = info["caption"]
            if display in seen_captions:
                continue
            seen_captions.add(display)

            formula = info["formula"]
            tableau_formula = _formula_legible(formula, defs) if formula else None
            try:
                expression = translate_formula(formula, resolve) if formula else resolve(fn)
            except NoTraducible as exc:
                # Se guarda la fórmula original; el compilador la rechaza y el
                # editor la muestra para convertirla a mano.
                expression = tableau_formula
                pendientes.append({
                    "name": display,
                    "kind": "dimension" if role == "dimension" else "metric",
                    "reason": str(exc),
                })

            if role == "dimension":
                dimensions.append({
                    "name": display,
                    "expression": expression,
                    "data_type": info["datatype"],
                    "description": "",
                    "tableau_formula": tableau_formula,
                })
            elif role == "measure":
                metrics.append({
                    "name": display,
                    "expression": expression,
                    "format": info["fmt"],
                    "description": "",
                    "tableau_formula": tableau_formula,
                })

        dimensions.sort(key=lambda x: x["name"])
        metrics.sort(key=lambda x: x["name"])

        avisos = []
        if len(ds_sql[ds_name]) > (1 if base_sql else 0):
            avisos.append("La fuente une consultas SQL personalizadas: se importó solo la primera como origen; "
                          "agregá las demás como joins con SQL en el editor.")
        if base_sql and twb_relaciones.parametros_sql(base_sql):
            avisos.append("La consulta SQL usa parámetros de Tableau: reemplazalos por un valor en el editor.")
        if ds_name in ds_proxy and not tables:
            avisos.append(
                "Fuente publicada en Tableau Server: el .twb no indica qué tablas del DW usa. "
                "Definí la tabla base y los joins en el editor del modelo."
            )

        results.append(TwbDatasource(
            model_name=_safe_name(caption),
            caption=caption,
            description=f"Importado desde Tableau Workbook (fuente: {caption})",
            base_table=tables[0] if tables else "",
            join_tables=tables[1:],
            dimensions=dimensions,
            metrics=metrics,
            pendientes=sorted(pendientes, key=lambda x: x["name"]),
            avisos=avisos,
            base_sql=base_sql,
        ))

    return results
