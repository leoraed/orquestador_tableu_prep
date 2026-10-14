"""
Compila y ejecuta consultas SQL a partir de un modelo semántico.
El LLM nunca escribe SQL libre: todo se deriva del modelo.
"""
import logging
import os
import re
import time

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
    r"IF\s+.+THEN",              # IF ... THEN
    r"\bIIF\s*\(",               # IIF(
    r"\bZN\s*\(",                # ZN(
    r"\bATTR\s*\(",              # ATTR(
    r"\bFIXED\s*\{",             # { FIXED ... }
    r"\bINCLUDE\s*\{",           # { INCLUDE ... }
    r"\bEXCLUDE\s*\{",           # { EXCLUDE ... }
    r"\bDATEPART\s*\(",          # DATEPARTNAME( (Tableau version)
    r"'\s*\}\s*\)",              # closing LOD expressions
]
_TABLEAU_RE = re.compile("|".join(_TABLEAU_PATTERNS), re.IGNORECASE)


def _check_tableau_expr(name: str, expression: str, kind: str):
    """Raise ConsultaError if expression looks like Tableau formula syntax."""
    if _TABLEAU_RE.search(expression):
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


def _build_sql(entity, dim_map, met_map, dimensiones, metricas, filtros, periodo, limite):
    params: list = []

    # SELECT
    selects = []
    for d in dimensiones:
        selects.append(f"  {_esc(dim_map[d].expression)} AS `{d}`")
    for m in metricas:
        selects.append(f"  {_esc(met_map[m].expression)} AS `{m}`")
    sql = "SELECT\n" + ",\n".join(selects) + "\n"

    # FROM
    sql += f"FROM `{entity.base_table}`\n"

    # JOINs
    for j in (entity.joins or []):
        table = j.get("table", "")
        alias = j.get("alias") or table
        on = j.get("on", "")
        jtype = j.get("type", "LEFT").upper()
        if not table:
            continue
        if on:
            sql += f"{jtype} JOIN `{table}` AS `{alias}` ON {on}\n"
        else:
            sql += f"{jtype} JOIN `{table}` AS `{alias}`\n"

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
            if desde and hasta:
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
) -> dict:
    """
    Ejecuta una consulta derivada del modelo semántico.

    Args:
        modelo:      Nombre del SemanticModel.
        metricas:    Lista de nombres de métricas declaradas en el modelo.
        dimensiones: Lista de nombres de dimensiones declaradas en el modelo.
        filtros:     {"dimension": valor} — solo dimensiones del modelo.
        periodo:     {"campo": "mes", "desde": "2024-01", "hasta": "2024-12"}
        limite:      Máximo de filas (tope: DW_MAX_ROWS).

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
        dim_map = {d.name: d for d in entity.dimensions}
        met_map = {mt.name: mt for mt in entity.metrics}

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
