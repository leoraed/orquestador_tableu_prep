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
from typing import Any

import uvicorn
import yaml as _yaml
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.sse import SseServerTransport
import mcp.types as types
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route

load_dotenv(Path(__file__).parent / ".env")

from src.catalog.db import SessionCatalog
from src.catalog.models_semantic import SemanticModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

server = Server("orquestador-semantico")


# ── helpers ─────────────────────────────────────────────────────────────────

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


# ── tools ────────────────────────────────────────────────────────────────────

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="listar_modelos",
            description=(
                "Lista todos los modelos semánticos disponibles. "
                "Úsalo primero para conocer qué modelos existen antes de consultar uno específico."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_modelo",
            description=(
                "Retorna el detalle completo de un modelo semántico: entidades, tablas involucradas, "
                "joins, dimensiones (con sus expresiones SQL/Tableau) y métricas. "
                "Úsalo para entender qué datos están disponibles en un modelo específico."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "nombre": {
                        "type": "string",
                        "description": "Nombre exacto del modelo semántico (obtenido con listar_modelos)",
                    }
                },
                "required": ["nombre"],
            },
        ),
        types.Tool(
            name="buscar_campos",
            description=(
                "Busca dimensiones y métricas por nombre o descripción en uno o todos los modelos. "
                "Útil para encontrar rápidamente qué campo usar para un concepto de negocio dado."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Texto a buscar (ej: 'churn', 'agente', 'activaciones')",
                    },
                    "modelo": {
                        "type": "string",
                        "description": "Nombre del modelo donde buscar (opcional; si se omite busca en todos)",
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_yaml",
            description=(
                "Exporta un modelo semántico completo en formato YAML. "
                "Úsalo cuando necesitás la definición completa para armar un tablero o análisis."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "nombre": {
                        "type": "string",
                        "description": "Nombre exacto del modelo semántico",
                    }
                },
                "required": ["nombre"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    db = SessionCatalog()
    try:
        if name == "listar_modelos":
            modelos = db.query(SemanticModel).order_by(SemanticModel.name).all()
            if not modelos:
                text = "No hay modelos semánticos definidos todavía."
            else:
                lines = ["Modelos semánticos disponibles:\n"]
                for m in modelos:
                    n_dims = sum(len(e.dimensions) for e in m.entities)
                    n_mets = sum(len(e.metrics) for e in m.entities)
                    desc = f" — {m.description}" if m.description else ""
                    lines.append(
                        f"- **{m.name}**: {len(m.entities)} entidad(es), "
                        f"{n_dims} dimensiones, {n_mets} métricas{desc}"
                    )
                text = "\n".join(lines)

        elif name == "get_modelo":
            nombre = arguments.get("nombre", "")
            m = db.query(SemanticModel).filter(SemanticModel.name == nombre).first()
            if not m:
                text = f"Modelo '{nombre}' no encontrado. Usá listar_modelos para ver los disponibles."
            else:
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
                text = "\n".join(lines)

        elif name == "buscar_campos":
            query = arguments.get("query", "").lower()
            modelo_filter = arguments.get("modelo")
            q = db.query(SemanticModel)
            if modelo_filter:
                q = q.filter(SemanticModel.name == modelo_filter)
            modelos = q.all()
            results = []
            for m in modelos:
                for e in m.entities:
                    for d in e.dimensions:
                        if query in d.name.lower() or (d.description and query in d.description.lower()):
                            results.append(f"[{m.name}] DIMENSIÓN  **{d.name}**: `{d.expression}`")
                    for mt in e.metrics:
                        if query in mt.name.lower() or (mt.description and query in mt.description.lower()):
                            results.append(f"[{m.name}] MÉTRICA    **{mt.name}**: `{mt.expression}`")
            if results:
                text = f"Campos que coinciden con '{query}':\n\n" + "\n".join(results)
            else:
                text = f"No se encontraron campos con '{query}'."

        elif name == "get_yaml":
            nombre = arguments.get("nombre", "")
            m = db.query(SemanticModel).filter(SemanticModel.name == nombre).first()
            if not m:
                text = f"Modelo '{nombre}' no encontrado. Usá listar_modelos para ver los disponibles."
            else:
                text = _yaml.dump(
                    _modelo_to_dict(m),
                    allow_unicode=True, default_flow_style=False, sort_keys=False
                )
        else:
            text = f"Herramienta desconocida: '{name}'"

        return [types.TextContent(type="text", text=text)]

    except Exception as exc:
        logger.exception("Error en tool %s", name)
        return [types.TextContent(type="text", text=f"Error interno: {exc}")]
    finally:
        db.close()


# ── SSE transport + Starlette app ────────────────────────────────────────────

sse = SseServerTransport("/messages/")


async def handle_sse(request: Request) -> None:
    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await server.run(
            streams[0], streams[1], server.create_initialization_options()
        )


app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
    ]
)


if __name__ == "__main__":
    logger.info("Iniciando MCP server en http://0.0.0.0:8001")
    logger.info("Endpoint SSE: http://0.0.0.0:8001/sse")
    uvicorn.run(app, host="0.0.0.0", port=8001)
