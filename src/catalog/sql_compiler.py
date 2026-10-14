"""
Compila y ejecuta consultas SQL a partir de un modelo semántico.
El LLM nunca escribe SQL libre: todo se deriva del modelo.
"""
import logging
import os
import re
import time
from datetime import date, timedelta
from types import SimpleNamespace

import pymysql
import pymysql.cursors

from src.catalog.db import SessionCatalog
from src.catalog.models_semantic import SemanticModel

logger = logging.getLogger(__name__)

_HOST = os.getenv("CATALOG_MYSQL_HOST", "localhost")
_PORT = int(os.getenv("CATALOG_MYSQL_PORT", "3306"))
_DB = os.getenv("CATALOG_MYSQL_DB", "orbith_bi")
_USER = os.getenv("DW_MYSQL_USER") or os.getenv("CATALOG_MYSQL_USER", "")
_PASSWORD = os.getenv("DW_MYSQL_PASSWORD") or os.getenv("CATALOG_MYSQL_PASSWORD", "")
_TIMEOUT = int(os.getenv("DW_QUERY_TIMEOUT_SEC", "30"))
_MAX_ROWS = int(os.getenv("DW_MAX_ROWS", "5000"))


class ConsultaError(Exception):
    pass


# Tableau formula patterns — these are NOT valid MySQL SQL
_TABLEAU_PATTERNS = [
    r"\[Calculation_\d+\]",      # [Calculation_123456]
    r"\bIF\s+[\s\S]+?\bTHEN\b",  # IF ... THEN (también en varias líneas)
    r"\bIIF\s*\(",               # IIF(
    r"\bZN\s*\(",                # ZN(
    r"\bATTR\s*\(",              # ATTR(
    r"\bFIXED\s*\{",             # { FIXED ... }
    r"\bINCLUDE\s*\{",           # { INCLUDE ... }
    r"\bEXCLUDE\s*\{",           # { EXCLUDE ... }
    r"\bDATEPART\s*\(",          # DATEPARTNAME( (Tableau version)
    r"'\s*\}\s*\)",              # closing LOD expressions
    r"\bCOUNTD\s*\(",            # COUNTD(
    r"\b(TODAY|DATETRUNC|DATEADD)\s*\(",
    r"\bELSEIF\b",
    r"\[[^\]\n']+\]",            # [campo] — MySQL no usa corchetes
    r"__tableau_internal_object_id__",
]
_TABLEAU_RE = re.compile("|".join(_TABLEAU_PATTERNS), re.IGNORECASE)


def es_sintaxis_tableau(expression: str) -> bool:
    return bool(_TABLEAU_RE.search(expression or ""))


def _check_tableau_expr(name: str, expression: str, kind: str):
    """Raise ConsultaError if expression looks like Tableau formula syntax."""
    if es_sintaxis_tableau(expression):
        raise ConsultaError(
            f"La {kind} '{name}' tiene una expresión en sintaxis Tableau "
            f"que no es SQL válido para MySQL:\n  {expression!r}\n"
            f"Editá la expresión en el editor de modelos con SQL válido "
            f"(ej: `tabla.columna` o `SUM(tabla.columna)`)."
        )


def _conn():
    return pymysql.connect(
        host=_HOST,
        port=_PORT,
        database=_DB,
        user=_USER,
        password=_PASSWORD,
        connect_timeout=_TIMEOUT,
        read_timeout=_TIMEOUT,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _render_sql(template: str, params: list) -> str:
    """Sustituye %s con los valores para mostrar en auditoría (no para ejecutar)."""
    rendered = template
    for p in params:
        if isinstance(p, str):
            val = f"'{p}'"
        elif p is None:
            val = "NULL"
        else:
            val = str(p)
        rendered = rendered.replace("%s", val, 1)
    return rendered


def _esc(expr: str) -> str:
    """Escape % in expressions so pymysql doesn't treat them as placeholders."""
    return expr.replace("%", "%%")


_DATE_TYPES = {"date", "datetime"}
_YM_RE = re.compile(r"^\d{4}-\d{2}$")
_YMD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _bound_date(valor: str, es_hasta: bool) -> str:
    """
    Convierte un límite de periodo (YYYY-MM o YYYY-MM-DD) a fecha YYYY-MM-DD.
    'desde' → primer día incluido; 'hasta' → primer día EXCLUIDO (rango semiabierto),
    así un campo datetime incluye todo el último día.
    """
    try:
        if _YM_RE.match(valor):
            d = date.fromisoformat(f"{valor}-01")
            if es_hasta:
                d = date(d.year + d.month // 12, d.month % 12 + 1, 1)
        elif _YMD_RE.match(valor):
            d = date.fromisoformat(valor)
            if es_hasta:
                d += timedelta(days=1)
        else:
            raise ValueError
    except ValueError:
        raise ConsultaError(
            f"Valor de periodo inválido: {valor!r}. Usá YYYY-MM o YYYY-MM-DD."
        )
    return d.isoformat()


def _from_sql(entity) -> str:
    """FROM + JOINs de la entidad."""
    sql = f"FROM `{entity.base_table}`\n"
    for j in (entity.joins or []):
        table = j.get("table", "")
        alias = j.get("alias") or table
        on = j.get("on", "")
        jtype = j.get("type", "LEFT").upper()
        if not table:
            continue
        if on:
            sql += f"{jtype} JOIN `{table}` AS `{alias}` ON {_esc(on)}\n"
        else:
            sql += f"{jtype} JOIN `{table}` AS `{alias}`\n"
    return sql


def _build_sql(entity, dim_map, met_map, dimensiones, metricas, filtros, periodo, limite):
    params: list = []

    # SELECT
    selects = []
    for d in dimensiones:
        selects.append(f"  {_esc(dim_map[d].expression)} AS `{_esc(d)}`")
    for m in metricas:
        selects.append(f"  {_esc(met_map[m].expression)} AS `{_esc(m)}`")
    sql = "SELECT\n" + ",\n".join(selects) + "\n"
    sql += _from_sql(entity)

    # WHERE
    where = []
    for campo, valor in (filtros or {}).items():
        where.append(f"  {_esc(dim_map[campo].expression)} = %s")
        params.append(valor)

    if periodo:
        campo = periodo.get("campo")
        desde = periodo.get("desde")
        hasta = periodo.get("hasta")
        if campo and campo in dim_map:
            expr = _esc(dim_map[campo].expression)
            if (dim_map[campo].data_type or "").lower() in _DATE_TYPES:
                if desde:
                    where.append(f"  {expr} >= %s")
                    params.append(_bound_date(desde, es_hasta=False))
                if hasta:
                    where.append(f"  {expr} < %s")
                    params.append(_bound_date(hasta, es_hasta=True))
            elif desde and hasta:
                where.append(f"  {expr} BETWEEN %s AND %s")
                params.extend([desde, hasta])
            elif desde:
                where.append(f"  {expr} >= %s")
                params.append(desde)
            elif hasta:
                where.append(f"  {expr} <= %s")
                params.append(hasta)

    if where:
        sql += "WHERE\n" + "\n  AND ".join(where) + "\n"

    # GROUP BY (solo si hay métricas — son agregados)
    if metricas and dimensiones:
        group_exprs = [_esc(dim_map[d].expression) for d in dimensiones]
        sql += "GROUP BY\n  " + ",\n  ".join(group_exprs) + "\n"

    # LIMIT
    sql += "LIMIT %s"
    params.append(min(limite, _MAX_ROWS))

    return sql, params


def consultar(
    modelo: str,
    metricas: list[str],
    dimensiones: list[str],
    filtros: dict | None = None,
    periodo: dict | None = None,
    limite: int = 1000,
    overrides: dict | None = None,
) -> dict:
    """
    Ejecuta una consulta derivada del modelo semántico.

    Args:
        modelo:      Nombre del SemanticModel.
        metricas:    Lista de nombres de métricas declaradas en el modelo.
        dimensiones: Lista de nombres de dimensiones declaradas en el modelo.
        filtros:     {"dimension": valor} — solo dimensiones del modelo.
        periodo:     {"campo": "Fecha", "desde": "2024-01", "hasta": "2024-12"}
                     Si el campo es date/datetime acepta YYYY-MM o YYYY-MM-DD
                     (ambos extremos inclusive); si no, compara el valor tal cual.
        limite:      Máximo de filas (tope: DW_MAX_ROWS).
        overrides:   {"dimensiones": {nombre: {"expression", "data_type"}},
                      "metricas": {nombre: {"expression"}}} — reemplaza o agrega
                     campos sin guardarlos (lo usa el editor para "Probar").

    Returns:
        {"sql": ..., "columnas": ..., "filas": ..., "total": ..., "tiempo_ms": ..., "error": null}
    """
    filtros = filtros or {}
    t0 = time.time()

    # ── Cargar modelo ────────────────────────────────────────────────────────
    db = SessionCatalog()
    try:
        m = db.query(SemanticModel).filter(SemanticModel.name == modelo).first()
        if not m:
            raise ConsultaError(
                f"Modelo '{modelo}' no encontrado. "
                f"Usá listar_modelos para ver los disponibles."
            )
        if not m.entities:
            raise ConsultaError(f"El modelo '{modelo}' no tiene entidades definidas.")

        entity = m.entities[0]
        if not entity.base_table:
            raise ConsultaError(
                f"El modelo '{modelo}' no tiene tabla base. Definila en el editor "
                f"de modelos (pestaña «Entidades y joins»)."
            )
        dim_map = {d.name: d for d in entity.dimensions}
        met_map = {mt.name: mt for mt in entity.metrics}
        for nombre, o in ((overrides or {}).get("dimensiones") or {}).items():
            dim_map[nombre] = SimpleNamespace(
                name=nombre, expression=o["expression"], data_type=o.get("data_type"))
        for nombre, o in ((overrides or {}).get("metricas") or {}).items():
            met_map[nombre] = SimpleNamespace(name=nombre, expression=o["expression"])

        # ── Validaciones ─────────────────────────────────────────────────────
        bad_mets = [x for x in metricas if x not in met_map]
        if bad_mets:
            raise ConsultaError(
                f"Métricas no declaradas en '{modelo}': {bad_mets}.\n"
                f"Disponibles: {sorted(met_map)}"
            )

        bad_dims = [x for x in dimensiones if x not in dim_map]
        if bad_dims:
            raise ConsultaError(
                f"Dimensiones no declaradas en '{modelo}': {bad_dims}.\n"
                f"Disponibles: {sorted(dim_map)}"
            )

        bad_filtros = [k for k in filtros if k not in dim_map]
        if bad_filtros:
            raise ConsultaError(
                f"Filtros con campos inválidos: {bad_filtros}. "
                f"Solo se permiten dimensiones del modelo."
            )

        if periodo and periodo.get("campo") and periodo["campo"] not in dim_map:
            raise ConsultaError(
                f"Campo de periodo '{periodo['campo']}' no es una dimensión del modelo."
            )

        # ── Validar que las expresiones sean SQL, no fórmulas Tableau ───────
        for d in dimensiones:
            _check_tableau_expr(d, dim_map[d].expression, "dimensión")
        for m_name in metricas:
            _check_tableau_expr(m_name, met_map[m_name].expression, "métrica")
        for k in filtros:
            _check_tableau_expr(k, dim_map[k].expression, "filtro/dimensión")

        # Multi-tenant: si el modelo tiene cliente_id, es obligatorio en filtros
        if "cliente_id" in dim_map and "cliente_id" not in filtros:
            raise ConsultaError(
                "Este modelo es multi-tenant: se requiere filtro por 'cliente_id'."
            )

        sql_template, params = _build_sql(
            entity, dim_map, met_map, dimensiones, metricas, filtros, periodo, limite
        )

    finally:
        db.close()

    # ── Ejecutar ─────────────────────────────────────────────────────────────
    conn = _conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql_template, params)
            rows = cursor.fetchall()
    except Exception as e:
        e.sql = _render_sql(sql_template, params)  # para mostrar en el editor
        raise
    finally:
        conn.close()

    elapsed = int((time.time() - t0) * 1000)
    columnas = dimensiones + metricas
    sql_legible = _render_sql(sql_template, params)

    logger.info(
        "consultar modelo=%s métricas=%s dimensiones=%s → %d filas en %dms",
        modelo, metricas, dimensiones, len(rows), elapsed,
    )
    logger.debug("SQL: %s", sql_legible)

    return {
        "sql": sql_legible,
        "columnas": columnas,
        "filas": [list(r.values()) for r in rows],
        "total": len(rows),
        "tiempo_ms": elapsed,
        "error": None,
    }


def columnas_tabla(tablas: list[str]) -> dict[str, list[str]]:
    """Columnas de las tablas del DW, para autocompletar en el editor."""
    tablas = [t for t in tablas if t]
    if not tablas:
        return {}
    marcadores = ", ".join(["%s"] * len(tablas))
    conn = _conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
                f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({marcadores}) "
                "ORDER BY TABLE_NAME, ORDINAL_POSITION",
                tablas,
            )
            rows = cursor.fetchall()
    finally:
        conn.close()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["TABLE_NAME"], []).append(r["COLUMN_NAME"])
    return out


def validar_campos(modelo: str) -> dict[str, str | None]:
    """
    Compila cada dimensión y métrica del modelo contra el DW sin traer datos
    (WHERE 1=0): MySQL valida columnas, joins y sintaxis igual, y responde en
    milisegundos. Los campos en sintaxis Tableau no se ejecutan.

    Devuelve {"dimension-<id>" | "metric-<id>": None si compila, o el error}.
    """
    db = SessionCatalog()
    try:
        m = db.query(SemanticModel).filter(SemanticModel.name == modelo).first()
        if not m:
            raise ConsultaError(f"Modelo '{modelo}' no encontrado.")
        pendientes = []
        for entity in m.entities:
            campos = [("dimension", c) for c in entity.dimensions] + [("metric", c) for c in entity.metrics]
            for kind, c in campos:
                if not es_sintaxis_tableau(c.expression):
                    pendientes.append((f"{kind}-{c.id}", c.expression,
                                       _from_sql(entity) if entity.base_table else None))
    finally:
        db.close()

    resultados: dict[str, str | None] = {}
    conn = _conn()
    try:
        with conn.cursor() as cursor:
            for clave, expr, from_sql in pendientes:
                if not from_sql:
                    resultados[clave] = "La entidad no tiene tabla base."
                    continue
                try:
                    cursor.execute(f"SELECT {_esc(expr)} AS v\n{from_sql}WHERE 1=0", ())
                    cursor.fetchall()
                    resultados[clave] = None
                except pymysql.MySQLError as e:
                    resultados[clave] = e.args[1] if len(e.args) > 1 else str(e)
    finally:
        conn.close()
    return resultados
