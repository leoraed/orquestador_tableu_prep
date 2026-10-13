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
from mcp.server.fastmcp import FastMCP

load_dotenv(Path(__file__).parent / ".env")

from src.catalog.db import SessionCatalog
from src.catalog.models_semantic import SemanticModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "orquestador-semantico",
    instructions=(
        "Tenés acceso a los modelos semánticos del Orquestador Tableau Prep. "
        "Empezá con listar_modelos para ver qué modelos hay, luego usá get_modelo o "
        "buscar_campos para encontrar las métricas y dimensiones relevantes."
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


# ── run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Iniciando MCP server en http://0.0.0.0:8001/sse")
    app = mcp.sse_app()
    uvicorn.run(app, host="0.0.0.0", port=8001)
