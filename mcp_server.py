"""
MCP Server para el Orquestador Tableau Prep.
Expone los modelos semánticos como herramientas para Claude.

Inicio:
    python mcp_server.py

Acceso: cada usuario de la app genera su token en /perfil (o un admin desde el servidor).
Toda conexión debe mandar "Authorization: Bearer <token>"; cada llamada a una
herramienta queda auditada con el usuario. MCP_AUTH=off la desactiva (solo desarrollo).

Configuración del cliente (Claude Desktop / Claude Code, vía mcp-remote):
    {
        "mcpServers": {
            "orquestador": {
                "command": "npx",
                "args": ["mcp-remote@latest", "http://<IP_SERVIDOR>:8001/sse", "--allow-http",
                         "--header", "Authorization:${ORQ_MCP_AUTH}"],
                "env": {"ORQ_MCP_AUTH": "Bearer <token>"}
            }
        }
    }
"""
import contextvars
import functools
import inspect
import json
import logging
import os
import time
from pathlib import Path

import anyio

import uvicorn
import yaml as _yaml
from dotenv import load_dotenv
try:
    from mcp.server.fastmcp import FastMCP as _MCPClass
except ModuleNotFoundError:
    from mcp.server.mcpserver import MCPServer as _MCPClass  # mcp >= 2.x rename

load_dotenv(Path(__file__).parent / ".env")

from src.catalog.db import BaseCatalog, SessionCatalog, engine_catalog
from src.catalog import models as _catalog_models  # noqa: F401  registra tablas del catálogo
from src.catalog.models_semantic import SemanticModel, migrar_columnas_semanticas
from src.catalog.models_tableros import Tablero
from src.catalog import tableros_service as _tableros
from src.catalog import tableros_vivos as _vivos
from src.catalog.sql_compiler import consultar as _consultar, ConsultaError
from src.catalog import models_acceso as _models_acceso  # noqa: F401  registra tablas de acceso
from src import usuarios as _usuarios
from src.permisos import HERRAMIENTAS_MCP, PERMISOS, permisos_de

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── acceso ───────────────────────────────────────────────────────────────────
# El usuario se identifica al abrir la conexión SSE; las herramientas corren en
# tareas hijas de esa conexión y heredan esta variable de contexto.
_usuario_actual: contextvars.ContextVar[_usuarios.SesionUsuario | None] = contextvars.ContextVar(
    "usuario_mcp", default=None)
_AUTH_DESACTIVADA = os.getenv("MCP_AUTH", "on").lower() in ("off", "0", "false", "no")


class AutenticacionMiddleware:
    """Exige 'Authorization: Bearer <token>' en cada pedido HTTP (conexión SSE y mensajes)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if _AUTH_DESACTIVADA:
            usuario = _usuarios.SesionUsuario(0, "anonimo", "anónimo (auth desactivada)", ("admin",),
                                              permisos_de(["admin"]))
        else:
            auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
            token = auth[7:].strip() if auth[:7].lower() == "bearer " else None
            usuario = await anyio.to_thread.run_sync(_verificar, token)
            if usuario is None:
                cuerpo = json.dumps({"error": "Token inválido o ausente. Pedí un token al administrador "
                                              "del orquestador."}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b'Bearer realm="orquestador"')]})
                await send({"type": "http.response.body", "body": cuerpo})
                return
        marca = _usuario_actual.set(usuario)
        try:
            await self.app(scope, receive, send)
        finally:
            _usuario_actual.reset(marca)


def _verificar(token: str | None) -> _usuarios.SesionUsuario | None:
    db = SessionCatalog()
    try:
        return _usuarios.verificar_token_mcp(db, token)
    finally:
        db.close()


def _usuario() -> _usuarios.SesionUsuario | None:
    return _usuario_actual.get()


def _es_error(resultado) -> bool:
    return isinstance(resultado, str) and resultado.startswith("Error")


def auditado(fn):
    """Exige el permiso de la herramienta (permisos.HERRAMIENTAS_MCP) y registra cada llamada:
    usuario, parámetros (resumidos), resultado y duración."""
    firma = inspect.signature(fn)
    permiso = HERRAMIENTAS_MCP[fn.__name__]

    @functools.wraps(fn)
    def envoltura(*args, **kwargs):
        u = _usuario()
        params = firma.bind_partial(*args, **kwargs)
        params.apply_defaults()
        t0 = time.time()
        ok, detalle = True, ""
        try:
            if u is None or not u.puede(permiso):
                resultado = (f"Error: tu usuario no tiene permiso para usar {fn.__name__} "
                             f"({PERMISOS[permiso]}). Pedíselo a un admin del orquestador.")
            else:
                resultado = fn(*args, **kwargs)
            if _es_error(resultado):
                ok, detalle = False, resultado.splitlines()[0]
            return resultado
        except Exception as e:
            ok, detalle = False, f"{type(e).__name__}: {e}"
            raise
        finally:
            db = SessionCatalog()
            try:
                _usuarios.auditar(db, "mcp", u.nombre if u else "desconocido", fn.__name__,
                                  dict(params.arguments), ok, detalle, int((time.time() - t0) * 1000))
            except Exception:
                logger.exception("No se pudo registrar la auditoría de %s", fn.__name__)
            finally:
                db.close()

    return envoltura

mcp = _MCPClass(
    "orquestador-semantico",
    instructions=(
        "Tenés acceso a los modelos semánticos del Orquestador Tableau Prep. "
        "Empezá con listar_modelos para ver qué modelos hay, luego usá get_modelo o "
        "buscar_campos para encontrar las métricas y dimensiones relevantes. "
        "Para ejecutar consultas usá consultar(), que compila SQL desde el modelo "
        "y devuelve filas + SQL auditado. Nunca escribas SQL libre. "
        "Si te piden un dashboard, armalo como un HTML autocontenido con datos de consultar() "
        "y publicalo con publicar_dashboard (revisá antes listar_dashboards para no duplicar)."
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
@auditado
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
@auditado
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
@auditado
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
@auditado
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
    "  dimensiones — lista de nombres de dimensiones para agrupar. Una dimensión de fecha\n"
    "                admite granularidad: 'Fecha:mes' (dia, semana, mes, trimestre, anio).\n"
    "  filtros     — dict {dimension: valor} o {dimension: [v1, v2]} (varios valores) (opcional).\n"
    "  periodo     — dict {campo, desde, hasta} o {campo, relativo} (opcional). desde/hasta en\n"
    "                YYYY-MM o YYYY-MM-DD, ambos inclusive. relativo: hoy, mes_actual,\n"
    "                mes_anterior, anio_actual, anio_anterior, ultimos_N_dias, ultimos_N_meses.\n"
    "  limite      — máximo de filas a devolver (default 1000, máx 5000)."
))
@auditado
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


# ── tableros (dashboards) ─────────────────────────────────────────────────────

def _url_tablero(slug: str, version: int | None = None) -> str:
    base = os.getenv("ORQUESTADOR_URL", "").rstrip("/")
    return f"{base}/tableros/{slug}" + (f"?version={version}" if version else "")


@mcp.tool(description=(
    "Publica un dashboard HTML en la sección Tableros del orquestador, donde el equipo lo puede "
    "ver y compartir. Devuelve el link.\n\n"
    "Dos tipos:\n"
    "  - VIVO (recomendado): pasás una `receta` con las consultas y los filtros; el orquestador las "
    "ejecuta cada vez que alguien abre el tablero y muestra una barra de filtros estándar arriba. "
    "El HTML NO trae datos: los recibe con\n"
    "      Tablero.alRecibirDatos(function (datos, filtros) { ... dibujar ... });\n"
    "    datos = {nombre_consulta: {columnas: [...], filas: [[...]], error}}; Tablero.filas(datos.x) "
    "las da como objetos. filtros = {id: {valor, etiqueta, texto}} (mostrá filtros.periodo.texto). "
    "Se llama de nuevo cada vez que cambian los filtros: el dibujo tiene que poder rehacerse. "
    "El objeto Tablero lo inyecta el orquestador; no lo definas.\n"
    "    Receta: {\"modelo\": m, \"filtros\": [{\"id\": \"periodo\", \"tipo\": \"periodo\", \"campo\": \"Fecha\", "
    "\"default\": \"ultimos_12_meses\"}, {\"id\": \"prov\", \"tipo\": \"dimension\", \"dimension\": \"Provincia\", "
    "\"multiple\": true}], \"consultas\": {\"mensual\": {\"metricas\": [...], \"dimensiones\": [\"Fecha:mes\"], "
    "\"filtros\": {\"Provincia\": \"$prov\", \"Cliente Id\": \"73\"}, \"periodo\": \"$periodo\"}}}. "
    "\"$id\" usa el valor del filtro; un período fijo va con \"campo_periodo\". Probala antes con probar_receta.\n"
    "  - FOTO: sin receta, el HTML trae los datos embebidos (sacados con consultar()) y no se actualiza.\n\n"
    "Requisitos del HTML (se muestra en un iframe aislado del orquestador):\n"
    "  - Un solo archivo autocontenido.\n"
    "  - NO usar localStorage, sessionStorage, cookies ni IndexedDB: están bloqueados.\n"
    "  - NO llamar a la API del orquestador ni a la del MCP: está bloqueado. Sí se pueden cargar "
    "librerías desde CDNs públicos, aunque es preferible no depender de ellos.\n"
    "  - Indicar en el propio tablero la fecha de los datos, la fuente y las salvedades.\n\n"
    "Antes de crear uno nuevo, usá listar_dashboards: si ya existe un tablero sobre el mismo tema, "
    "publicá una versión nueva pasando su slug en `actualizar` (las versiones anteriores se conservan). "
    "Solo podés actualizar tableros que publicaste vos (o cualquiera, si sos admin); si no, creá uno nuevo.\n\n"
    "Parámetros:\n"
    "  titulo      — título visible (obligatorio al crear; al actualizar, si se pasa, reemplaza el actual).\n"
    "  html        — documento HTML completo.\n"
    "  descripcion — qué muestra y para quién (1-2 oraciones).\n"
    "  modelo      — modelo semántico del que salen los datos (nombre exacto).\n"
    "  tags        — separados por coma, ej: 'noc, operadores, mensual'.\n"
    "  datos_hasta — fecha del último dato incluido, AAAA-MM-DD.\n"
    "  actualizar  — slug de un tablero existente para publicar una versión nueva.\n"
    "  nota        — qué cambió en esta versión (al actualizar).\n"
    "  receta      — consultas y filtros (tablero vivo). Sin receta, el tablero es una foto."
))
@auditado
def publicar_dashboard(
    titulo: str,
    html: str,
    descripcion: str = "",
    modelo: str = "",
    tags: str = "",
    datos_hasta: str = "",
    actualizar: str = "",
    nota: str = "",
    receta: dict | None = None,
) -> str:
    u = _usuario()
    if u is None:
        return "Error: no hay un usuario autenticado; no se puede publicar."
    db = SessionCatalog()
    try:
        if modelo and not db.query(SemanticModel).filter(SemanticModel.name == modelo).first():
            return f"Error: el modelo «{modelo}» no existe. Usá listar_modelos para ver los nombres exactos."
        if receta:
            receta = _vivos.validar_receta(db, receta)
            if "Tablero.alRecibirDatos" not in html:
                return ("Error: un tablero vivo tiene que recibir los datos con "
                        "Tablero.alRecibirDatos(function (datos, filtros) {...}); el HTML no lo usa.")
            prueba = _vivos.ejecutar(receta, _vivos.valores_efectivos(receta, {}), consultar_fn=_consultar)
            errores = {k: d["error"] for k, d in prueba["datos"].items() if d["error"]}
            if errores:
                return "Error: no publiqué; estas consultas fallan con los filtros por defecto:\n" + \
                       "\n".join(f"  - {k}: {e}" for k, e in errores.items())
            modelo = modelo or receta.get("modelo") or ""
        if actualizar:
            t = _tableros.buscar(db, actualizar)
            if not t:
                return f"Error: no existe un tablero con slug «{actualizar}». Usá listar_dashboards."
            if not _tableros.puede_publicar(t, u.nombre, u.puede("tableros.administrar")):
                duenio = f"es de «{t.autor}»" if t.autor else "se subió desde la app"
                return (f"Error: el tablero «{t.titulo}» {duenio}; solo su autor o un admin pueden publicarle "
                        f"versiones nuevas. Si querés una variante, publicá un tablero nuevo.")
            if titulo or descripcion or modelo or tags:
                _tableros.editar(t, titulo or t.titulo, descripcion or (t.descripcion or ""),
                                 modelo or (t.modelo or ""), tags or (t.tags or ""))
            _tableros.nueva_version(db, t, html, datos_hasta, nota or "Publicado desde Claude", autor=u.nombre,
                                    receta=receta or _vivos.receta_heredada(t, html))
            receta = t.ultima.receta
            accion = f"Publiqué la versión {t.ultima.version} de «{t.titulo}»"
        else:
            t = _tableros.crear(db, titulo, html, descripcion, modelo, tags, datos_hasta,
                                nota="Publicado desde Claude", autor=u.nombre, receta=receta or None)
            accion = f"Publiqué el tablero «{t.titulo}»"
        db.commit()
        tipo = "vivo: se actualiza al abrirlo" if receta else "foto: los datos quedan fijos"
        return (f"{accion} ({tipo}).\nLink: {_url_tablero(t.slug)}\n"
                f"Slug: {t.slug} (usalo en `actualizar` para nuevas versiones).")
    except _tableros.TableroError as e:
        db.rollback()
        return f"Error: {e}"
    finally:
        db.close()


@mcp.tool(description=(
    "Lista los tableros publicados en el orquestador (slug, título, modelo, versión y fecha de los "
    "datos). Opcionalmente filtra por modelo semántico."
))
@auditado
def listar_dashboards(modelo: str = "") -> str:
    db = SessionCatalog()
    try:
        q = db.query(Tablero).order_by(Tablero.updated_at.desc())
        if modelo:
            q = q.filter(Tablero.modelo == modelo)
        tableros = [_tableros.a_dict(t) for t in q.all()]
    finally:
        db.close()
    if not tableros:
        return "No hay tableros publicados" + (f" del modelo «{modelo}»." if modelo else ".")
    lines = ["Tableros publicados:\n"]
    for t in tableros:
        extra = " · ".join(x for x in [t["modelo"] and f"modelo {t['modelo']}", f"v{t['version']}",
                                       t["datos_hasta"] and f"datos al {t['datos_hasta']}",
                                       f"autor {t['autor']}" if t["autor"] else "subido desde la app"] if x)
        lines.append(f"- **{t['titulo']}** (`{t['slug']}`) — {extra}"
                     + (f"\n  {t['descripcion']}" if t["descripcion"] else ""))
    return "\n".join(lines)


@mcp.tool(description=(
    "Devuelve el HTML de un tablero publicado (la última versión, o la indicada) con sus datos, "
    "para modificarlo y volver a publicarlo con publicar_dashboard(actualizar=slug)."
))
@auditado
def obtener_dashboard(slug: str, version: int = 0) -> str:
    db = SessionCatalog()
    try:
        t = _tableros.buscar(db, slug)
        if not t:
            return f"Error: no existe un tablero con slug «{slug}». Usá listar_dashboards."
        v = next((x for x in t.versiones if x.version == version), None) if version else t.ultima
        if v is None:
            return f"Error: el tablero «{slug}» no tiene versión {version}."
        info = _tableros.a_dict(t)
        cabecera = (f"Tablero «{t.titulo}» (`{t.slug}`), versión {v.version} de {info['version']}"
                    + (f", datos al {v.datos_hasta.isoformat()}" if v.datos_hasta else "")
                    + (f", modelo {t.modelo}" if t.modelo else "") + ".")
        receta = (f"\n\nReceta (tablero vivo):\n```json\n{json.dumps(v.receta, ensure_ascii=False, indent=1)}\n```"
                  if v.receta else "")
        return f"{cabecera}{receta}\n\n```html\n{v.html}\n```"
    finally:
        db.close()


@mcp.tool(description=(
    "Ejecuta una receta de tablero vivo sin publicarla y muestra qué devuelve cada consulta "
    "(columnas, cantidad de filas y las primeras filas). Usala para armar y verificar la receta "
    "antes de publicar_dashboard. `valores` permite probar filtros distintos de los por defecto, "
    "ej. {\"periodo\": \"ultimos_3_meses\", \"prov\": [\"Córdoba\"]}."
))
@auditado
def probar_receta(receta: dict, valores: dict | None = None) -> str:
    db = SessionCatalog()
    try:
        receta = _vivos.validar_receta(db, receta)
    except _tableros.TableroError as e:
        return f"Error en la receta: {e}"
    finally:
        db.close()
    vals = _vivos.valores_efectivos(receta, valores)
    r = _vivos.ejecutar(receta, vals, consultar_fn=_consultar)
    lineas = ["Filtros usados: " + json.dumps(vals, ensure_ascii=False)]
    for nombre, d in r["datos"].items():
        if d["error"]:
            lineas.append(f"\n### {nombre}: ERROR\n{d['error']}")
            continue
        lineas.append(f"\n### {nombre}: {len(d['filas'])} filas\n" + " | ".join(d["columnas"]))
        lineas += [" | ".join(str(x) for x in f) for f in d["filas"][:8]]
        if len(d["filas"]) > 8:
            lineas.append(f"… ({len(d['filas']) - 8} filas más)")
    return "\n".join(lineas)


# ── run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Tablas y columnas nuevas del catálogo (por si el MCP arranca antes que main.py)
    BaseCatalog.metadata.create_all(bind=engine_catalog)
    migrar_columnas_semanticas(engine_catalog)
    _db = SessionCatalog()
    try:
        _usuarios.migrar_usuarios_mcp(_db)
        _db.commit()
    finally:
        _db.close()
    if _AUTH_DESACTIVADA:
        logger.warning("MCP_AUTH=off: el servidor acepta conexiones SIN token. Usar solo en desarrollo.")
    logger.info("Iniciando MCP server en http://0.0.0.0:8001/sse")
    app = AutenticacionMiddleware(mcp.sse_app(host="0.0.0.0"))
    uvicorn.run(app, host="0.0.0.0", port=8001)
