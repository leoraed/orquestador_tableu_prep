"""
MCP Server para el Orquestador Tableau Prep.
Expone los modelos semánticos como herramientas para Claude.

Inicio:
    python mcp_server.py

Configuración en Claude Desktop (claude_desktop_config.json):
    {
        "mcpServers": {
            "orquestador": {
                "url": "http://<IP_SERVIDOR>:8001/sse"
            }
        }
    }
"""
import logging
from pathlib import Path

import uvicorn
import yaml as _yaml
from dotenv import load_dotenv
try:
    from mcp.server.fastmcp import FastMCP as _MCPClass
except ModuleNotFoundError:
    from mcp.server.mcpserver import MCPServer as _MCPClass  # mcp >= 2.x rename

load_dotenv(Path(__file__).parent / ".env")

from src.catalog.db import SessionCatalog, engine_catalog
from src.catalog.models_semantic import SemanticModel, migrar_columnas_semanticas
from src.catalog.sql_compiler import consultar as _consultar, ConsultaError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

migrar_columnas_semanticas(engine_catalog)

mcp = _MCPClass(
    "orquestador-semantico",
    instructions=(
        "Tenés acceso a los modelos semánticos del Orquestador Tableau Prep. "
        "Empezá con listar_modelos para ver qué modelos hay, luego usá get_modelo o "
        "buscar_campos para encontrar las métricas y dimensiones relevantes. "
        "Para ejecutar consultas usá consultar(), que compila SQL desde el modelo "
        "y devuelve filas + SQL auditado. Nunca escribas SQL libre."
    ),
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _modelo_to_dict(m: SemanticModel) -> dict:
    return {
        "name": m.name,
        "description": m.description or "",
        "entities": [
            {
                "name": e.name,
                "base_table": e.base_table,
                "joins": e.joins or [],
                "dimensions": [
                    {"name": d.name, "expression": d.expression,
                     "data_type": d.data_type or "string", "description": d.description or ""}
                    for d in e.dimensions
                ],
                "metrics": [
                    {"name": mt.name, "expression": mt.expression,
                     "format": mt.format or "number", "description": mt.description or ""}
                    for mt in e.metrics
                ],
            }
            for e in m.entities
        ],
    }


# ── tools ─────────────────────────────────────────────────────────────────────

@mcp.tool(description=(
    "Lista todos los modelos semánticos disponibles con un resumen de entidades, "
    "dimensiones y métricas. Úsalo primero para saber qué modelos existen."
))
def listar_modelos() -> str:
    db = SessionCatalog()
    try:
        modelos = db.query(SemanticModel).order_by(SemanticModel.name).all()
        if not modelos:
            return "No hay modelos semánticos definidos todavía."
        lines = ["Modelos semánticos disponibles:\n"]
        for m in modelos:
            n_dims = sum(len(e.dimensions) for e in m.entities)
            n_mets = sum(len(e.metrics) for e in m.entities)
            desc = f" — {m.description}" if m.description else ""
            lines.append(
                f"- **{m.name}**: {len(m.entities)} entidad(es), "
                f"{n_dims} dimensiones, {n_mets} métricas{desc}"
            )
        return "\n".join(lines)
    finally:
        db.close()


@mcp.tool(description=(
    "Retorna el detalle completo de un modelo semántico: entidades, tablas, joins, "
    "dimensiones y métricas con sus expresiones. "
    "Úsalo para entender qué datos tiene un modelo específico."
))
def get_modelo(nombre: str) -> str:
    """
    Args:
        nombre: Nombre exacto del modelo semántico (obtenido con listar_modelos).
    """
    db = SessionCatalog()
    try:
        m = db.query(SemanticModel).filter(SemanticModel.name == nombre).first()
        if not m:
            return f"Modelo '{nombre}' no encontrado. Usá listar_modelos para ver los disponibles."
        d = _modelo_to_dict(m)
        lines = [f"# Modelo semántico: {d['name']}"]
        if d["description"]:
            lines.append(d["description"])
        lines.append("")
        for e in d["entities"]:
            lines.append(f"## Entidad: {e['name']}")
            lines.append(f"- Tabla base: `{e['base_table']}`")
            if e["joins"]:
                join_list = ", ".join(
                    f"`{j['table']}`" + (f" ON {j['on']}" if j.get("on") else "")
                    for j in e["joins"]
                )
                lines.append(f"- Joins ({len(e['joins'])}): {join_list}")
            lines.append(f"\n### Dimensiones ({len(e['dimensions'])})")
            for dim in e["dimensions"]:
                desc = f" — {dim['description']}" if dim.get("description") else ""
                lines.append(f"- **{dim['name']}** `{dim['data_type']}`: `{dim['expression']}`{desc}")
            lines.append(f"\n### Métricas ({len(e['metrics'])})")
            for met in e["metrics"]:
                desc = f" — {met['description']}" if met.get("description") else ""
                lines.append(f"- **{met['name']}** `{met['format']}`: `{met['expression']}`{desc}")
        return "\n".join(lines)
    finally:
        db.close()


@mcp.tool(description=(
    "Busca dimensiones y métricas por nombre o descripción en uno o todos los modelos. "
    "Útil para encontrar rápidamente qué campo usar para un concepto de negocio."
))
def buscar_campos(query: str, modelo: str = "") -> str:
    """
    Args:
        query: Texto a buscar (ej: 'churn', 'agente', 'activaciones').
        modelo: Nombre del modelo donde buscar (opcional; vacío = busca en todos).
    """
    db = SessionCatalog()
    try:
        q = db.query(SemanticModel)
        if modelo:
            q = q.filter(SemanticModel.name == modelo)
        modelos = q.all()
        needle = query.lower()
        results = []
        for m in modelos:
            for e in m.entities:
                for d in e.dimensions:
                    if needle in d.name.lower() or (d.description and needle in d.description.lower()):
                        results.append(f"[{m.name}] DIMENSIÓN  **{d.name}**: `{d.expression}`")
                for mt in e.metrics:
                    if needle in mt.name.lower() or (mt.description and needle in mt.description.lower()):
                        results.append(f"[{m.name}] MÉTRICA    **{mt.name}**: `{mt.expression}`")
        if results:
            return f"Campos que coinciden con '{query}':\n\n" + "\n".join(results)
        return f"No se encontraron campos con '{query}'."
    finally:
        db.close()


@mcp.tool(description=(
    "Exporta un modelo semántico completo en formato YAML. "
    "Úsalo cuando necesitás la definición completa para armar un tablero o análisis."
))
def get_yaml(nombre: str) -> str:
    """
    Args:
        nombre: Nombre exacto del modelo semántico.
    """
    db = SessionCatalog()
    try:
        m = db.query(SemanticModel).filter(SemanticModel.name == nombre).first()
        if not m:
            return f"Modelo '{nombre}' no encontrado. Usá listar_modelos para ver los disponibles."
        return _yaml.dump(
            _modelo_to_dict(m),
            allow_unicode=True, default_flow_style=False, sort_keys=False
        )
    finally:
        db.close()


@mcp.tool(description=(
    "Ejecuta una consulta sobre el DW a partir del modelo semántico. "
    "El SQL se compila automáticamente; nunca escribas SQL libre. "
    "Devuelve filas, columnas y el SQL generado para auditoría.\n\n"
    "Parámetros:\n"
    "  modelo      — nombre exacto del modelo (obtenelo con listar_modelos).\n"
    "  metricas    — lista de nombres de métricas declaradas en el modelo.\n"
    "  dimensiones — lista de nombres de dimensiones para agrupar.\n"
    "  filtros     — dict {dimension: valor} para filtrar (opcional).\n"
    "  periodo     — dict {campo, desde, hasta} (opcional). Si campo es una dimensión\n"
    "                date/datetime (ej. Fecha), desde/hasta van en YYYY-MM o YYYY-MM-DD,\n"
    "                ambos inclusive. Si es string (ej. mes), se compara tal cual.\n"
    "  limite      — máximo de filas a devolver (default 1000, máx 5000)."
))
def consultar(
    modelo: str,
    metricas: list[str],
    dimensiones: list[str],
    filtros: dict | None = None,
    periodo: dict | None = None,
    limite: int = 1000,
) -> str:
    """
    Args:
        modelo:      Nombre del modelo semántico.
        metricas:    Ej: ["Ventas", "Leads"].
        dimensiones: Ej: ["mes", "provincia"].
        filtros:     Ej: {"provincia": "Buenos Aires"}.
        periodo:     Ej: {"campo": "Fecha", "desde": "2024-01", "hasta": "2024-12"}.
        limite:      Máximo de filas (tope interno: 5000).
    """
    try:
        result = _consultar(
            modelo=modelo,
            metricas=metricas,
            dimensiones=dimensiones,
            filtros=filtros,
            periodo=periodo,
            limite=limite,
        )
    except ConsultaError as e:
        return f"Error de validación: {e}"
    except Exception as e:
        logger.exception("consultar: error inesperado")
        return f"Error al ejecutar la consulta: {e}"

    lines = [
        f"**SQL generado:**\n```sql\n{result['sql']}\n```\n",
        f"**{result['total']} filas** en {result['tiempo_ms']} ms\n",
    ]

    if result["filas"]:
        header = " | ".join(result["columnas"])
        sep = " | ".join("---" for _ in result["columnas"])
        rows_md = "\n".join(
            " | ".join(str(v) for v in row) for row in result["filas"][:50]
        )
        lines.append(f"{header}\n{sep}\n{rows_md}")
        if result["total"] > 50:
            lines.append(f"\n*(mostrando 50 de {result['total']} filas)*")
    else:
        lines.append("*Sin resultados para los filtros aplicados.*")

    return "\n".join(lines)


# ── run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Iniciando MCP server en http://0.0.0.0:8001/sse")
    app = mcp.sse_app(host="0.0.0.0")
    uvicorn.run(app, host="0.0.0.0", port=8001)
