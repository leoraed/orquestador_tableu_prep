import logging
from contextlib import asynccontextmanager
from datetime import datetime, date, time as dt_time
from typing import Optional
from pathlib import Path
from urllib.parse import quote
import yaml

import uuid
from typing import List
from fastapi import FastAPI, Depends, BackgroundTasks, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from src.database import get_db
from src.models import EjecucionFlow
from src.runner import ejecutar_flow, _procesos_activos, _cancelados, _lock, _matar_proceso
from src.config import load_flows, load_pipelines, load_tasks, load_settings, load_carpetas, descubrir_tfl, BASE_DIR, _load_yaml, _save_yaml
from src.scheduler import inicializar_scheduler, detener_scheduler, recargar_scheduler, scheduler, _disparar_pipeline

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
logger = logging.getLogger(__name__)


def _duracion(inicio: datetime, fin: Optional[datetime]) -> str:
    if not fin:
        return "—"
    total = int((fin - inicio).total_seconds())
    if total < 60:
        return f"{total}s"
    m, s = divmod(total, 60)
    return f"{m}m {s}s"


templates.env.globals["duracion"] = _duracion


@asynccontextmanager
async def lifespan(app: FastAPI):
    inicializar_scheduler()
    yield
    detener_scheduler()


app = FastAPI(title="Orquestador Tableau Prep", version="2.0.0", lifespan=lifespan)


# ── helpers ────────────────────────────────────────────────────────────────

def _redir(path: str, msg: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?msg={quote(msg)}", status_code=303)


def _guardar_flows(flows: list[dict]) -> None:
    data = _load_yaml()
    data["flows"] = flows
    _save_yaml(data)


def _guardar_pipelines(pipelines: list[dict]) -> None:
    data = _load_yaml()
    data["pipelines"] = pipelines
    _save_yaml(data)


def _guardar_tasks(tasks: list[dict]) -> None:
    data = _load_yaml()
    data["tasks"] = tasks
    _save_yaml(data)


def _guardar_carpetas(carpetas: list[str]) -> None:
    data = _load_yaml()
    data["carpetas"] = carpetas
    _save_yaml(data)


def _guardar_settings(s: dict) -> None:
    path = BASE_DIR / "config" / "settings.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(s, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _jobs_map() -> dict[str, str]:
    """Mapea pipeline_name → próxima ejecución más cercana entre todos sus schedules."""
    result: dict[str, str] = {}
    for job in scheduler.get_jobs():
        if "__s" not in job.id or job.id.startswith("__"):
            continue
        pipeline_name = job.id.rsplit("__s", 1)[0]
        if job.next_run_time:
            nrt = str(job.next_run_time)
            if pipeline_name not in result or nrt < result[pipeline_name]:
                result[pipeline_name] = nrt
    return result


# ── páginas web ────────────────────────────────────────────────────────────

@app.get("/")
def dashboard(request: Request, msg: str = None, db: Session = Depends(get_db)):
    flows = load_flows()
    pipelines = load_pipelines()
    ejecuciones = db.query(EjecucionFlow).order_by(EjecucionFlow.inicio.desc()).limit(10).all()
    inicio_hoy = datetime.combine(date.today(), dt_time.min)
    ejecuciones_hoy = db.query(EjecucionFlow).filter(EjecucionFlow.inicio >= inicio_hoy).count()
    exitosas_hoy = db.query(EjecucionFlow).filter(
        EjecucionFlow.inicio >= inicio_hoy,
        EjecucionFlow.estado == "exitoso",
    ).count()
    return templates.TemplateResponse(request, "dashboard.html", {
        "flows": flows,
        "pipelines": pipelines,
        "ejecuciones": ejecuciones,
        "ejecuciones_hoy": ejecuciones_hoy,
        "exitosas_hoy": exitosas_hoy,
        "jobs": _jobs_map(),
        "mensaje": msg,
        "active": "dashboard",
    })


@app.get("/flows")
def pagina_flows(request: Request, msg: str = None):
    flows = load_flows()
    pipelines = load_pipelines()
    carpetas = load_carpetas()
    descubiertos = descubrir_tfl(carpetas)
    paths_registrados = {f["file"] for f in flows}
    todos_nombres = [f["name"] for f in flows]
    return templates.TemplateResponse(request, "flows.html", {
        "flows": flows,
        "pipelines": pipelines,
        "carpetas": carpetas,
        "descubiertos": descubiertos,
        "paths_registrados": paths_registrados,
        "todos_nombres": todos_nombres,
        "mensaje": msg,
        "active": "flows",
    })


def _info_ejecucion(ej) -> dict | None:
    if not ej:
        return None
    inicio_hoy = datetime.combine(date.today(), dt_time.min)
    return {
        "estado": ej.estado,
        "es_hoy": ej.inicio >= inicio_hoy,
        "fecha": ej.inicio.strftime("%d/%m/%Y %H:%M"),
    }


@app.get("/grafo")
def pagina_grafo(request: Request, pipeline: str = None, db: Session = Depends(get_db)):
    flows = load_flows()
    tasks = load_tasks()
    pipelines = load_pipelines()

    flows_en_grafo = flows
    tasks_en_grafo = tasks
    deps_en_pipeline: dict[str, list[str]] = {}
    pipeline_seleccionado = None

    if pipeline:
        pip = next((p for p in pipelines if p["name"] == pipeline), None)
        if pip:
            pipeline_seleccionado = pip["name"]
            nombres = {s["flow"] for s in pip.get("flows", [])}
            flows_en_grafo = [f for f in flows if f["name"] in nombres]
            tasks_en_grafo = [t for t in tasks if t["name"] in nombres]
            for step in pip.get("flows", []):
                deps_en_pipeline[step["flow"]] = step.get("depends_on", [])

    ultimos_estados = {}
    for item in list(flows_en_grafo) + list(tasks_en_grafo):
        ej = db.query(EjecucionFlow).filter(
            EjecucionFlow.nombre_flow == item["name"]
        ).order_by(EjecucionFlow.inicio.desc()).first()
        info = _info_ejecucion(ej)
        if info:
            ultimos_estados[item["name"]] = info

    return templates.TemplateResponse(request, "grafo.html", {
        "flows": flows_en_grafo,
        "tasks": tasks_en_grafo,
        "deps_en_pipeline": deps_en_pipeline,
        "pipeline_seleccionado": pipeline_seleccionado,
        "pipelines": pipelines,
        "ultimos_estados": ultimos_estados,
        "active": "grafo",
    })


@app.get("/api/grafo/estados")
def api_grafo_estados(pipeline: str = None, db: Session = Depends(get_db)):
    flows = load_flows()
    tasks = load_tasks()
    if pipeline:
        pips = {p["name"]: p for p in load_pipelines()}
        pip = pips.get(pipeline)
        if pip:
            nombres = {s["flow"] for s in pip.get("flows", [])}
            flows = [f for f in flows if f["name"] in nombres]
            tasks = [t for t in tasks if t["name"] in nombres]
    result = {}
    for item in list(flows) + list(tasks):
        ej = db.query(EjecucionFlow).filter(
            EjecucionFlow.nombre_flow == item["name"]
        ).order_by(EjecucionFlow.inicio.desc()).first()
        result[item["name"]] = _info_ejecucion(ej)
    return result


@app.get("/pipelines")
def pagina_pipelines(request: Request, msg: str = None):
    flows = load_flows()
    tasks = load_tasks()
    pipelines = load_pipelines()
    jobs = _jobs_map()
    return templates.TemplateResponse(request, "pipelines.html", {
        "flows": flows,
        "tasks": tasks,
        "pipelines": pipelines,
        "jobs": jobs,
        "mensaje": msg,
        "active": "pipelines",
    })


@app.get("/tasks")
def pagina_tasks(request: Request, msg: str = None):
    return templates.TemplateResponse(request, "tasks.html", {
        "tasks": load_tasks(),
        "mensaje": msg,
        "active": "tasks",
    })


@app.get("/configuracion")
def pagina_configuracion(request: Request, msg: str = None):
    return templates.TemplateResponse(request, "configuracion.html", {
        "settings": load_settings(),
        "mensaje": msg,
        "active": "config",
    })


@app.get("/logs")
def pagina_logs(request: Request, lineas: int = 200):
    log_path = BASE_DIR / "logs" / "orquestador.log"
    contenido = []
    if log_path.exists():
        with open(log_path, encoding="utf-8", errors="replace") as f:
            contenido = f.readlines()
        contenido = contenido[-lineas:]
    return templates.TemplateResponse(request, "logs.html", {
        "lineas": contenido,
        "lineas_n": lineas,
        "active": "logs",
    })


@app.get("/historial")
def pagina_historial(request: Request, flow: str = None, db: Session = Depends(get_db)):
    q = db.query(EjecucionFlow).order_by(EjecucionFlow.inicio.desc())
    if flow:
        q = q.filter(EjecucionFlow.nombre_flow == flow)
    return templates.TemplateResponse(request, "historial.html", {
        "ejecuciones": q.limit(200).all(),
        "flows": load_flows(),
        "filtro_flow": flow or "",
        "active": "historial",
    })


# ── CRUD flows ─────────────────────────────────────────────────────────────

@app.post("/api/flows")
def api_crear_flow(
    name: str = Form(...),
    file: str = Form(...),
    enabled: Optional[str] = Form(None),
    credentials: Optional[str] = Form(None),
    reintentos: int = Form(default=0),
    reintento_espera_min: int = Form(default=5),
):
    flows = load_flows()
    if any(f["name"] == name.strip() for f in flows):
        return _redir("/flows", f"Error: ya existe un flow '{name}'.")
    flows.append({
        "name": name.strip(),
        "file": file.strip(),
        "enabled": enabled is not None,
        "credentials": credentials.strip() if credentials and credentials.strip() else None,
        "reintentos": reintentos,
        "reintento_espera_min": reintento_espera_min,
    })
    _guardar_flows(flows)
    return _redir("/flows", f"Flow '{name}' creado exitosamente.")


@app.post("/api/flows/{nombre}/editar")
def api_editar_flow(
    nombre: str,
    name: str = Form(...),
    file: str = Form(...),
    enabled: Optional[str] = Form(None),
    credentials: Optional[str] = Form(None),
    reintentos: int = Form(default=0),
    reintento_espera_min: int = Form(default=5),
):
    flows = load_flows()
    idx = next((i for i, f in enumerate(flows) if f["name"] == nombre), None)
    if idx is None:
        return _redir("/flows", f"Error: flow '{nombre}' no encontrado.")

    new_name = name.strip()
    old_name = nombre

    flows[idx] = {
        "name": new_name,
        "file": file.strip(),
        "enabled": enabled is not None,
        "credentials": credentials.strip() if credentials and credentials.strip() else None,
        "reintentos": reintentos,
        "reintento_espera_min": reintento_espera_min,
    }
    _guardar_flows(flows)

    # Si cambió el nombre, actualizar referencias en pipelines
    if new_name != old_name:
        pipelines = load_pipelines()
        for pipeline in pipelines:
            for step in pipeline.get("flows", []):
                if step["flow"] == old_name:
                    step["flow"] = new_name
                step["depends_on"] = [
                    new_name if d == old_name else d
                    for d in step.get("depends_on", [])
                ]
        _guardar_pipelines(pipelines)

    recargar_scheduler()
    return _redir("/flows", f"Flow '{new_name}' actualizado.")


@app.post("/api/flows/{nombre}/eliminar")
def api_eliminar_flow(nombre: str):
    flows = load_flows()
    nuevos = [f for f in flows if f["name"] != nombre]
    if len(nuevos) == len(flows):
        return _redir("/flows", f"Error: flow '{nombre}' no encontrado.")
    _guardar_flows(nuevos)

    # Limpiar referencias en pipelines
    pipelines = load_pipelines()
    for pipeline in pipelines:
        pipeline["flows"] = [s for s in pipeline.get("flows", []) if s["flow"] != nombre]
        for step in pipeline["flows"]:
            step["depends_on"] = [d for d in step.get("depends_on", []) if d != nombre]
    _guardar_pipelines(pipelines)

    recargar_scheduler()
    return _redir("/flows", f"Flow '{nombre}' eliminado.")


@app.post("/api/flows/{nombre}/ejecutar")
def api_ejecutar_manual(nombre: str, background_tasks: BackgroundTasks):
    flows_map = {f["name"]: f for f in load_flows()}
    if nombre not in flows_map:
        return _redir("/", f"Error: flow '{nombre}' no encontrado.")
    flow = flows_map[nombre]
    background_tasks.add_task(
        ejecutar_flow,
        nombre=flow["name"],
        archivo=flow["file"],
        credenciales=flow.get("credentials"),
        disparador="manual",
        grupo_id=str(uuid.uuid4()),
        reintentos=flow.get("reintentos", 0),
        reintento_espera_min=flow.get("reintento_espera_min", 5),
        pipeline_name=None,
    )
    return _redir("/", f"Flow '{nombre}' iniciado manualmente (sin pipeline).")


# ── CRUD pipelines ─────────────────────────────────────────────────────────

@app.post("/api/pipelines")
async def api_crear_pipeline(request: Request):
    data = await request.json()
    pipelines = load_pipelines()
    nombre = (data.get("name") or "").strip()
    if not nombre:
        return JSONResponse({"ok": False, "error": "El nombre no puede estar vacío."})
    if any(p["name"] == nombre for p in pipelines):
        return JSONResponse({"ok": False, "error": f"Ya existe un pipeline '{nombre}'."})
    pipelines.append({
        "name": nombre,
        "schedules": data.get("schedules", []),
        "flows": data.get("flows", []),
    })
    _guardar_pipelines(pipelines)
    recargar_scheduler()
    return JSONResponse({"ok": True})


@app.post("/api/pipelines/{nombre}/editar")
async def api_editar_pipeline(nombre: str, request: Request):
    data = await request.json()
    pipelines = load_pipelines()
    idx = next((i for i, p in enumerate(pipelines) if p["name"] == nombre), None)
    if idx is None:
        return JSONResponse({"ok": False, "error": f"Pipeline '{nombre}' no encontrado."})
    pipelines[idx] = {
        "name": (data.get("name") or nombre).strip(),
        "schedules": data.get("schedules", []),
        "flows": data.get("flows", []),
    }
    _guardar_pipelines(pipelines)
    recargar_scheduler()
    return JSONResponse({"ok": True})


@app.post("/api/pipelines/{nombre}/eliminar")
def api_eliminar_pipeline(nombre: str):
    pipelines = load_pipelines()
    nuevos = [p for p in pipelines if p["name"] != nombre]
    if len(nuevos) == len(pipelines):
        return _redir("/pipelines", f"Error: pipeline '{nombre}' no encontrado.")
    _guardar_pipelines(nuevos)
    recargar_scheduler()
    return _redir("/pipelines", f"Pipeline '{nombre}' eliminado.")


@app.post("/api/pipelines/{nombre}/ejecutar")
def api_ejecutar_pipeline(nombre: str, background_tasks: BackgroundTasks):
    pipelines = {p["name"]: p for p in load_pipelines()}
    if nombre not in pipelines:
        return _redir("/pipelines", f"Error: pipeline '{nombre}' no encontrado.")
    background_tasks.add_task(_disparar_pipeline, pipeline_name=nombre)
    return _redir("/pipelines", f"Pipeline '{nombre}' iniciado manualmente.")


@app.post("/api/pipelines/{pipeline_name}/steps/{step_name}/ejecutar")
def api_ejecutar_step_en_pipeline(pipeline_name: str, step_name: str, background_tasks: BackgroundTasks):
    flows_map = {f["name"]: f for f in load_flows()}
    tasks_map = {t["name"]: t for t in load_tasks()}
    grupo_id = str(uuid.uuid4())

    if step_name in flows_map:
        flow = flows_map[step_name]
        background_tasks.add_task(
            ejecutar_flow,
            nombre=flow["name"],
            archivo=flow["file"],
            credenciales=flow.get("credentials"),
            disparador="manual",
            grupo_id=grupo_id,
            reintentos=flow.get("reintentos", 0),
            reintento_espera_min=flow.get("reintento_espera_min", 5),
            pipeline_name=pipeline_name,
        )
    elif step_name in tasks_map:
        from src.tasks import ejecutar_task
        task = tasks_map[step_name]
        background_tasks.add_task(
            ejecutar_task,
            task=task,
            disparador="manual",
            grupo_id=grupo_id,
            pipeline_name=pipeline_name,
        )
    else:
        return JSONResponse({"ok": False, "error": f"Step '{step_name}' no encontrado."})

    return JSONResponse({"ok": True})


@app.post("/api/pipelines/{pipeline_name}/flows/{flow_name}/deps")
async def api_editar_deps_en_pipeline(pipeline_name: str, flow_name: str, request: Request):
    data = await request.json()
    nuevas_deps = data.get("depends_on", [])
    pipelines = load_pipelines()
    idx = next((i for i, p in enumerate(pipelines) if p["name"] == pipeline_name), None)
    if idx is None:
        return JSONResponse({"ok": False, "error": f"Pipeline '{pipeline_name}' no encontrado."})
    for step in pipelines[idx].get("flows", []):
        if step["flow"] == flow_name:
            step["depends_on"] = nuevas_deps
            _guardar_pipelines(pipelines)
            return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": f"Flow '{flow_name}' no está en el pipeline."})


# ── CRUD tasks ─────────────────────────────────────────────────────────────

@app.post("/api/tasks")
async def api_crear_task(request: Request):
    data = await request.json()
    tasks = load_tasks()
    nombre = (data.get("name") or "").strip()
    if not nombre:
        return JSONResponse({"ok": False, "error": "El nombre no puede estar vacío."})
    flows = load_flows()
    if any(f["name"] == nombre for f in flows) or any(t["name"] == nombre for t in tasks):
        return JSONResponse({"ok": False, "error": f"Ya existe un flow o tarea con el nombre '{nombre}'."})
    tasks.append({
        "name": nombre,
        "type": data.get("type", "tableau_cloud"),
        "resource_type": data.get("resource_type", "workbook"),
        "resource_name": data.get("resource_name", "").strip(),
        "enabled": data.get("enabled", True),
    })
    _guardar_tasks(tasks)
    return JSONResponse({"ok": True})


@app.post("/api/tasks/{nombre}/editar")
async def api_editar_task(nombre: str, request: Request):
    data = await request.json()
    tasks = load_tasks()
    idx = next((i for i, t in enumerate(tasks) if t["name"] == nombre), None)
    if idx is None:
        return JSONResponse({"ok": False, "error": f"Tarea '{nombre}' no encontrada."})
    new_name = (data.get("name") or nombre).strip()
    tasks[idx] = {
        "name": new_name,
        "type": data.get("type", "tableau_cloud"),
        "resource_type": data.get("resource_type", "workbook"),
        "resource_name": data.get("resource_name", "").strip(),
        "enabled": data.get("enabled", True),
    }
    _guardar_tasks(tasks)
    # If name changed, update pipeline references
    if new_name != nombre:
        pipelines = load_pipelines()
        for pipeline in pipelines:
            for step in pipeline.get("flows", []):
                if step["flow"] == nombre:
                    step["flow"] = new_name
                step["depends_on"] = [new_name if d == nombre else d for d in step.get("depends_on", [])]
        _guardar_pipelines(pipelines)
    return JSONResponse({"ok": True})


@app.post("/api/tasks/{nombre}/eliminar")
async def api_eliminar_task(nombre: str):
    tasks = load_tasks()
    nuevas = [t for t in tasks if t["name"] != nombre]
    if len(nuevas) == len(tasks):
        return JSONResponse({"ok": False, "error": f"Tarea '{nombre}' no encontrada."})
    _guardar_tasks(nuevas)
    # Remove from pipelines
    pipelines = load_pipelines()
    for pipeline in pipelines:
        pipeline["flows"] = [s for s in pipeline.get("flows", []) if s["flow"] != nombre]
        for step in pipeline["flows"]:
            step["depends_on"] = [d for d in step.get("depends_on", []) if d != nombre]
    _guardar_pipelines(pipelines)
    return JSONResponse({"ok": True})


@app.post("/api/tasks/{nombre}/ejecutar")
def api_ejecutar_task_manual(nombre: str, background_tasks: BackgroundTasks):
    from src.tasks import ejecutar_task
    tasks_map = {t["name"]: t for t in load_tasks()}
    if nombre not in tasks_map:
        return _redir("/tasks", f"Error: tarea '{nombre}' no encontrada.")
    task = tasks_map[nombre]
    background_tasks.add_task(
        ejecutar_task,
        task=task,
        disparador="manual",
        grupo_id=str(uuid.uuid4()),
        pipeline_name=None,
    )
    return _redir("/tasks", f"Tarea '{nombre}' iniciada manualmente.")


# ── carpetas ───────────────────────────────────────────────────────────────

@app.post("/api/carpetas")
def api_agregar_carpeta(path: str = Form(...)):
    carpetas = load_carpetas()
    path = path.strip()
    if not path:
        return _redir("/flows", "Error: el path no puede estar vacío.")
    from pathlib import Path as _Path
    if not _Path(path).exists():
        return _redir("/flows", f"Error: la carpeta '{path}' no existe.")
    if path in carpetas:
        return _redir("/flows", f"Error: la carpeta ya está registrada.")
    carpetas.append(path)
    _guardar_carpetas(carpetas)
    return _redir("/flows", f"Carpeta agregada: {path}")


@app.post("/api/carpetas/eliminar")
def api_eliminar_carpeta(path: str = Form(...)):
    carpetas = load_carpetas()
    nuevas = [c for c in carpetas if c != path]
    _guardar_carpetas(nuevas)
    return _redir("/flows", "Carpeta eliminada.")


# ── configuración ──────────────────────────────────────────────────────────

@app.post("/api/configuracion")
def api_guardar_configuracion(
    prep_cli_path: str = Form(...),
    timezone: str = Form(...),
    timeout_segundos: int = Form(...),
    database_url: str = Form(...),
    ttl_grupo_horas: float = Form(...),
    telegram_bot_token: Optional[str] = Form(None),
    telegram_chat_id: Optional[str] = Form(None),
    telegram_resumen_cron: Optional[str] = Form(None),
    tableau_cloud_server: Optional[str] = Form(None),
    tableau_cloud_site: Optional[str] = Form(None),
    tableau_cloud_token_name: Optional[str] = Form(None),
    tableau_cloud_token_value: Optional[str] = Form(None),
):
    s = load_settings()
    s["prep_cli_path"] = prep_cli_path.strip()
    s["timezone"] = timezone.strip()
    s["timeout_segundos"] = timeout_segundos
    s["database_url"] = database_url.strip()
    s["ttl_grupo_horas"] = ttl_grupo_horas
    s["telegram_bot_token"] = (telegram_bot_token or "").strip()
    s["telegram_chat_id"] = (telegram_chat_id or "").strip()
    s["telegram_resumen_cron"] = (telegram_resumen_cron or "").strip()
    s["tableau_cloud"] = {
        "server": (tableau_cloud_server or "").strip(),
        "site": (tableau_cloud_site or "").strip(),
        "token_name": (tableau_cloud_token_name or "").strip(),
        "token_value": (tableau_cloud_token_value or "").strip(),
    }
    _guardar_settings(s)
    recargar_scheduler()
    return _redir("/configuracion", "Configuración guardada.")


@app.post("/api/ejecuciones/{eid}/cancelar")
def api_cancelar_ejecucion(eid: int, db: Session = Depends(get_db)):
    ej = db.query(EjecucionFlow).filter(EjecucionFlow.id == eid).first()
    if not ej:
        return _redir("/historial", f"Error: ejecución #{eid} no encontrada.")
    if ej.estado != "en_proceso":
        return _redir("/historial", f"La ejecución #{eid} ya terminó ({ej.estado}).")

    with _lock:
        _cancelados.add(eid)
        proc = _procesos_activos.get(eid)

    if proc:
        _matar_proceso(proc)
        logger.info(f"Proceso #{eid} terminado por cancelación manual (taskkill /F /T).")
    else:
        ej.estado = "cancelado"
        ej.fin = datetime.utcnow()
        ej.error = "Cancelado manualmente."
        db.commit()

    return _redir("/historial", f"Ejecución #{eid} cancelada.")


# ── API JSON ───────────────────────────────────────────────────────────────

@app.get("/api/ejecuciones")
def api_ejecuciones(limit: int = 50, db: Session = Depends(get_db)):
    return db.query(EjecucionFlow).order_by(EjecucionFlow.inicio.desc()).limit(limit).all()


@app.get("/api/ejecuciones/{eid}")
def api_ejecucion_detalle(eid: int, db: Session = Depends(get_db)):
    ej = db.query(EjecucionFlow).filter(EjecucionFlow.id == eid).first()
    if not ej:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="No encontrada")
    return {
        "id": ej.id,
        "nombre_flow": ej.nombre_flow,
        "salida": ej.salida,
        "error": ej.error,
        "estado": ej.estado,
    }


@app.get("/health")
def health():
    return {"estado": "ok", "timestamp": datetime.utcnow().isoformat()}


# ── Catálogo ───────────────────────────────────────────────────────────────

from src.catalog.db import get_catalog_db
from src.catalog.models import CatalogFlow, CatalogSource, CatalogField, CatalogOutput, CatalogTransformation
from src.catalog.service import parse_todas_las_carpetas, parse_and_save as catalog_parse_file


from sqlalchemy import func as _func, or_ as _or
from sqlalchemy.orm import selectinload as _selectinload
from src.catalog.lineage import CAPAS, capa as _capa, construir_indice


def _linaje_catalogo(db: Session):
    """Flows con fuentes/outputs cargados + índice de qué flow escribe/lee cada tabla."""
    flows = (db.query(CatalogFlow)
             .options(_selectinload(CatalogFlow.sources), _selectinload(CatalogFlow.outputs))
             .order_by(CatalogFlow.flow_name).all())
    indice, fuentes, escritas = construir_indice(flows)
    # Modelos semánticos que usan cada tabla como base
    modelos_por_tabla: dict[str, list[str]] = {}
    for m_name, tabla in (db.query(SemanticModel.name, SemanticEntity.base_table)
                          .join(SemanticEntity, SemanticEntity.model_id == SemanticModel.id).all()):
        if tabla:
            modelos_por_tabla.setdefault(tabla.lower(), []).append(m_name)
    return flows, indice, fuentes, escritas, modelos_por_tabla


def _fmt_fecha(d) -> str:
    return d.strftime("%d/%m/%Y %H:%M") if d else "—"


@app.get("/catalogo")
def pagina_catalogo(request: Request, db: Session = Depends(get_catalog_db)):
    flows, indice, fuentes, escritas, modelos = _linaje_catalogo(db)
    campos_por_flow = dict(db.query(CatalogField.flow_id, _func.count(CatalogField.id))
                           .group_by(CatalogField.flow_id).all())
    total_campos = sum(campos_por_flow.values())
    documentados = (db.query(_func.count(CatalogField.id))
                    .filter(CatalogField.description.isnot(None), CatalogField.description != "")
                    .scalar())

    flows_data = [{
        "name": f.flow_name,
        "capa": _capa(f.flow_name),
        "parsed_at": _fmt_fecha(f.parsed_at),
        "tipos": sorted({s.source_type for s in f.sources if s.source_type}),
        "campos": campos_por_flow.get(f.id, 0),
        "lee": [r.etiqueta for r in fuentes[f.flow_name]],
        "escribe": escritas[f.flow_name],
    } for f in flows]

    tablas = sorted(set(indice.escriben) | set(indice.leen))
    tablas_data = [{
        "tabla": t,
        "escriben": indice.escriben.get(t, []),
        "leen": indice.leen.get(t, []),
        "modelos": modelos.get(t, []),
    } for t in tablas]

    return templates.TemplateResponse(request, "catalogo.html", {
        "datos": {
            "capas": CAPAS,
            "flows": flows_data,
            "tablas": tablas_data,
            "stats": {"flows": len(flows), "tablas": len(tablas),
                      "campos": total_campos, "documentados": documentados},
        },
        "active": "catalogo",
    })


@app.get("/api/catalog/buscar-campos")
def api_catalog_buscar_campos(q: str = "", tipo: str = "", limite: int = 200,
                              db: Session = Depends(get_catalog_db)):
    """Busca campos por nombre o descripción, con su fuente y flow en una sola consulta."""
    q = q.strip()
    if len(q) < 2:
        return {"campos": [], "total": 0}
    patron = f"%{q}%"
    query = (db.query(CatalogField, CatalogSource, CatalogFlow.flow_name)
             .join(CatalogSource, CatalogField.source_id == CatalogSource.id)
             .join(CatalogFlow, CatalogField.flow_id == CatalogFlow.id)
             .filter(_or(CatalogField.field_name.ilike(patron),
                         CatalogField.description.ilike(patron))))
    if tipo:
        query = query.filter(CatalogSource.source_type == tipo)
    total = query.count()
    filas = query.order_by(CatalogField.field_name, CatalogFlow.flow_name).limit(min(limite, 500)).all()
    return {
        "total": total,
        "campos": [{
            "id": f.id,
            "name": f.field_name,
            "type": f.field_type,
            "description": f.description,
            "flow": flow_name,
            "fuente": s.source_table or s.node_name,
            "source_type": s.source_type,
        } for f, s, flow_name in filas],
    }


@app.get("/catalogo/{flow_name}")
def pagina_catalogo_flow(flow_name: str, request: Request, db: Session = Depends(get_catalog_db)):
    from fastapi import HTTPException
    flows, indice, fuentes, escritas, modelos = _linaje_catalogo(db)
    flow = next((f for f in flows if f.flow_name == flow_name), None)
    if not flow:
        raise HTTPException(status_code=404, detail="Flow no encontrado en el catálogo.")

    otros = lambda lista: [x for x in lista if x != flow_name]
    lee = [{
        "etiqueta": r.etiqueta,
        "tabla": r.tabla,
        "inferida": r.inferida,
        "productores": otros(indice.escriben.get(r.tabla, [])) if r.tabla else [],
    } for r in fuentes[flow_name]]
    escribe = [{
        "tabla": t,
        "lectores": otros(indice.leen.get(t, [])),
        "modelos": modelos.get(t, []),
    } for t in escritas[flow_name]]

    campos = (db.query(CatalogField).filter(CatalogField.flow_id == flow.id)
              .order_by(CatalogField.source_id, CatalogField.ordinal).all())
    por_fuente: dict[int, list] = {}
    for c in campos:
        por_fuente.setdefault(c.source_id, []).append({
            "id": c.id, "name": c.field_name, "type": c.field_type, "ordinal": c.ordinal,
            "description": c.description or "", "tags": c.tags or "",
        })
    sources = [{
        "id": s.id,
        "node_name": s.node_name,
        "tabla": s.source_table or (s.file_path or "").replace("\\", "/").split("/")[-1] or None,
        "source_type": s.source_type,
        "database": s.database_name,
        "fields": por_fuente.get(s.id, []),
    } for s in sorted(flow.sources, key=lambda s: s.node_name.lower())]

    transforms = (db.query(CatalogTransformation)
                  .filter(CatalogTransformation.flow_id == flow.id).all())

    return templates.TemplateResponse(request, "catalogo_flow.html", {
        "flow": {"flow_name": flow.flow_name, "capa": _capa(flow.flow_name),
                 "parsed_at": _fmt_fecha(flow.parsed_at), "file_path": flow.file_path},
        "datos": {
            "lee": lee,
            "escribe": escribe,
            "sources": sources,
            "transforms": [{"name": t.node_name, "type": t.transform_type, "details": t.details}
                           for t in transforms],
        },
        "graph_data": flow.graph_data or {"nodes": [], "edges": []},
        "active": "catalogo",
    })


@app.post("/api/catalog/parse")
def api_catalog_parse(background_tasks: BackgroundTasks):
    carpetas = load_carpetas()
    if not carpetas:
        return JSONResponse({"ok": False, "error": "No hay carpetas configuradas."})

    def _run():
        db = next(get_catalog_db())
        try:
            results = parse_todas_las_carpetas(carpetas, db)
            logger.info(f"Catálogo: {len(results)} archivos procesados.")
        finally:
            db.close()

    background_tasks.add_task(_run)
    return JSONResponse({"ok": True, "mensaje": f"Parseando {len(carpetas)} carpeta(s) en background."})


@app.get("/api/catalog/flows")
def api_catalog_flows(db: Session = Depends(get_catalog_db)):
    flows = db.query(CatalogFlow).order_by(CatalogFlow.flow_name).all()
    return [
        {
            "id": f.id,
            "flow_name": f.flow_name,
            "file_path": f.file_path,
            "parsed_at": f.parsed_at.isoformat() if f.parsed_at else None,
            "sources": len(f.sources),
            "outputs": len(f.outputs),
        }
        for f in flows
    ]


@app.get("/api/catalog/flows/{flow_name}")
def api_catalog_flow_detalle(flow_name: str, db: Session = Depends(get_catalog_db)):
    from fastapi import HTTPException
    flow = db.query(CatalogFlow).filter(CatalogFlow.flow_name == flow_name).first()
    if not flow:
        raise HTTPException(status_code=404, detail="Flow no encontrado en el catálogo.")
    return {
        "id": flow.id,
        "flow_name": flow.flow_name,
        "file_path": flow.file_path,
        "parsed_at": flow.parsed_at.isoformat() if flow.parsed_at else None,
        "sources": [
            {
                "id": s.id,
                "node_name": s.node_name,
                "source_type": s.source_type,
                "source_table": s.source_table,
                "server": s.server,
                "database_name": s.database_name,
                "file_path": s.file_path,
                "fields": [
                    {
                        "id": f.id,
                        "name": f.field_name,
                        "type": f.field_type,
                        "ordinal": f.ordinal,
                        "description": f.description,
                        "tags": f.tags,
                    }
                    for f in s.fields
                ],
            }
            for s in flow.sources
        ],
        "outputs": [
            {"node_name": o.node_name, "destination_table": o.destination_table}
            for o in flow.outputs
        ],
        "transforms": [
            {"name": t.node_name, "type": t.transform_type, "details": t.details}
            for t in flow.transforms
        ],
    }


@app.get("/api/catalog/fields")
def api_catalog_fields(
    q: str = None,
    source_type: str = None,
    db: Session = Depends(get_catalog_db),
):
    query = db.query(CatalogField)
    if q:
        query = query.filter(CatalogField.field_name.ilike(f"%{q}%"))
    if source_type:
        query = query.join(CatalogSource).filter(CatalogSource.source_type == source_type)
    fields = query.limit(500).all()
    return [
        {
            "id": f.id,
            "flow_id": f.flow_id,
            "flow_name": f.source.flow.flow_name if f.source and f.source.flow else None,
            "field_name": f.field_name,
            "field_type": f.field_type,
            "description": f.description,
            "tags": f.tags,
            "source_table": f.source.source_table if f.source else None,
            "source_type": f.source.source_type if f.source else None,
        }
        for f in fields
    ]


@app.patch("/api/catalog/fields/{field_id}")
async def api_catalog_field_enriquecer(field_id: int, request: Request, db: Session = Depends(get_catalog_db)):
    from fastapi import HTTPException
    data = await request.json()
    field = db.query(CatalogField).filter(CatalogField.id == field_id).first()
    if not field:
        raise HTTPException(status_code=404, detail="Campo no encontrado.")
    if "description" in data:
        field.description = data["description"]
    if "tags" in data:
        field.tags = data["tags"]
    db.commit()
    return {"ok": True}


# ── Modelos Semánticos ─────────────────────────────────────────────────────

from datetime import datetime as _dt
import yaml as _yaml
from fastapi.responses import Response
from src.catalog.models_semantic import SemanticModel, SemanticEntity, SemanticDimension, SemanticMetric
from src.catalog.models import CatalogOutput


def _modelo_to_dict(m: SemanticModel) -> dict:
    return {
        "name": m.name,
        "description": m.description or "",
        "entities": [
            {
                "name": e.name,
                "description": e.description or "",
                "base_table": e.base_table,
                "joins": e.joins or [],
                "dimensions": [
                    {"name": d.name, "expression": d.expression,
                     "data_type": d.data_type or "string", "description": d.description or "",
                     "tableau_formula": d.tableau_formula}
                    for d in e.dimensions
                ],
                "metrics": [
                    {"name": mt.name, "expression": mt.expression,
                     "format": mt.format or "number", "description": mt.description or "",
                     "tableau_formula": mt.tableau_formula}
                    for mt in e.metrics
                ],
            }
            for e in m.entities
        ],
    }


def _save_entities(modelo: SemanticModel, entities_data: list, db: Session):
    for e_data in entities_data:
        entity = SemanticEntity(
            model_id=modelo.id,
            name=(e_data.get("name") or "").strip(),
            description=(e_data.get("description") or "").strip() or None,
            base_table=(e_data.get("base_table") or "").strip(),
            joins=e_data.get("joins") or [],
        )
        db.add(entity)
        db.flush()
        for d in e_data.get("dimensions") or []:
            db.add(SemanticDimension(
                entity_id=entity.id,
                name=(d.get("name") or "").strip(),
                expression=(d.get("expression") or "").strip(),
                data_type=d.get("data_type") or "string",
                description=(d.get("description") or "").strip() or None,
                tableau_formula=d.get("tableau_formula"),
            ))
        for mt in e_data.get("metrics") or []:
            db.add(SemanticMetric(
                entity_id=entity.id,
                name=(mt.get("name") or "").strip(),
                expression=(mt.get("expression") or "").strip(),
                format=mt.get("format") or "number",
                description=(mt.get("description") or "").strip() or None,
                tableau_formula=mt.get("tableau_formula"),
            ))


@app.get("/modelos")
def pagina_modelos(request: Request, db: Session = Depends(get_catalog_db)):
    modelos = db.query(SemanticModel).order_by(SemanticModel.name).all()
    total_entidades = sum(len(m.entities) for m in modelos)
    total_metricas = sum(len(e.metrics) for m in modelos for e in m.entities)
    modelos_data = []
    for m in modelos:
        modelos_data.append({
            **m.__dict__,
            "entities": m.entities,
            "total_dims": sum(len(e.dimensions) for e in m.entities),
            "total_metrics": sum(len(e.metrics) for e in m.entities),
        })
    return templates.TemplateResponse(request, "modelos.html", {
        "modelos": modelos_data,
        "total_entidades": total_entidades,
        "total_metricas": total_metricas,
        "active": "modelos",
    })


@app.get("/modelos/{nombre}")
def pagina_modelo_editor(nombre: str, request: Request, db: Session = Depends(get_catalog_db)):
    from fastapi import HTTPException
    modelo = db.query(SemanticModel).filter(SemanticModel.name == nombre).first()
    if not modelo:
        raise HTTPException(status_code=404, detail="Modelo no encontrado.")
    tablas = [r[0] for r in db.query(CatalogOutput.destination_table)
              .filter(CatalogOutput.destination_table.isnot(None))
              .distinct().order_by(CatalogOutput.destination_table).all()]
    return templates.TemplateResponse(request, "modelos_editor.html", {
        "modelo": modelo,
        "tablas_catalogo": tablas,
        "estado_inicial": {
            "name": modelo.name,
            "description": modelo.description or "",
            "entities": [_entidad_to_dict(e) for e in modelo.entities],
            "campos": _campos_modelo(modelo),
        },
        "active": "modelos",
    })


@app.post("/api/modelos")
async def api_crear_modelo(request: Request, db: Session = Depends(get_catalog_db)):
    data = await request.json()
    nombre = (data.get("name") or "").strip()
    if not nombre:
        return JSONResponse({"ok": False, "error": "El nombre es obligatorio."})
    if db.query(SemanticModel).filter(SemanticModel.name == nombre).first():
        return JSONResponse({"ok": False, "error": f"Ya existe un modelo '{nombre}'."})
    modelo = SemanticModel(
        name=nombre,
        description=(data.get("description") or "").strip() or None,
        created_at=_dt.utcnow(),
        updated_at=_dt.utcnow(),
    )
    db.add(modelo)
    db.commit()
    return JSONResponse({"ok": True})


@app.post("/api/modelos/{nombre}/editar")
async def api_editar_modelo(nombre: str, request: Request, db: Session = Depends(get_catalog_db)):
    from fastapi import HTTPException
    data = await request.json()
    modelo = db.query(SemanticModel).filter(SemanticModel.name == nombre).first()
    if not modelo:
        raise HTTPException(status_code=404, detail="Modelo no encontrado.")
    modelo.name = (data.get("name") or nombre).strip()
    modelo.description = (data.get("description") or "").strip() or None
    modelo.updated_at = _dt.utcnow()
    for e in list(modelo.entities):
        db.delete(e)
    db.flush()
    _save_entities(modelo, data.get("entities") or [], db)
    db.commit()
    db.refresh(modelo)
    return JSONResponse({"ok": True, "modelo": _modelo_to_dict(modelo)})


@app.post("/api/modelos/{nombre}/eliminar")
def api_eliminar_modelo(nombre: str, db: Session = Depends(get_catalog_db)):
    modelo = db.query(SemanticModel).filter(SemanticModel.name == nombre).first()
    if not modelo:
        return JSONResponse({"ok": False, "error": "Modelo no encontrado."})
    db.delete(modelo)
    db.commit()
    return JSONResponse({"ok": True})


# ── Editor de modelos: endpoints por campo ────────────────────────────────────
# Cada dimensión/métrica se crea, edita y borra por separado (el endpoint
# /editar reemplaza el modelo completo y pisa ediciones concurrentes).

from fastapi.encoders import jsonable_encoder
from src.catalog.sql_compiler import (
    consultar as _consultar_dw, ConsultaError, es_sintaxis_tableau, columnas_tabla, validar_campos,
)
from src.catalog.tableau_to_sql import NoTraducible, make_field_resolver, translate_formula

_KINDS = {"dimension": SemanticDimension, "metric": SemanticMetric}
_JOIN_TYPES = {"LEFT", "INNER", "RIGHT"}


def _modelo_o_404(db: Session, nombre: str) -> SemanticModel:
    from fastapi import HTTPException
    modelo = db.query(SemanticModel).filter(SemanticModel.name == nombre).first()
    if not modelo:
        raise HTTPException(status_code=404, detail="Modelo no encontrado.")
    return modelo


def _campo_to_dict(c, kind: str) -> dict:
    expr = c.expression or ""
    pendiente = es_sintaxis_tableau(expr)
    return {
        "id": c.id,
        "kind": kind,
        "entity_id": c.entity_id,
        "name": c.name,
        "expression": expr,
        "data_type": getattr(c, "data_type", None),
        "format": getattr(c, "format", None),
        "description": c.description or "",
        # Modelos importados antes de esta columna: la expresión pendiente ES la fórmula
        "tableau_formula": c.tableau_formula or (expr if pendiente else None),
        "estado": "pendiente" if pendiente else "sql",
    }


def _entidad_to_dict(e: SemanticEntity) -> dict:
    return {"id": e.id, "name": e.name, "description": e.description or "",
            "base_table": e.base_table, "joins": e.joins or []}


def _campos_modelo(modelo: SemanticModel) -> list[dict]:
    campos = []
    for e in modelo.entities:
        campos += [_campo_to_dict(d, "dimension") for d in e.dimensions]
        campos += [_campo_to_dict(m, "metric") for m in e.metrics]
    return campos


def _validar_campo(entity: SemanticEntity, kind: str, data: dict, actual=None) -> str | None:
    name = (data.get("name") or "").strip()
    expr = (data.get("expression") or "").strip()
    if not name:
        return "El nombre es obligatorio."
    if not expr:
        return "La expresión es obligatoria."
    hermanos = entity.dimensions if kind == "dimension" else entity.metrics
    if any(c.name == name and (actual is None or c.id != actual.id) for c in hermanos):
        tipo = "una dimensión" if kind == "dimension" else "una métrica"
        return f"Ya existe {tipo} llamada '{name}' en esta entidad."
    # Se permite guardar una expresión Tableau sin tocar (ej. solo cambia la descripción)
    if es_sintaxis_tableau(expr) and not (actual is not None and actual.expression == expr):
        return ("La expresión está en sintaxis Tableau y el compilador la va a rechazar. "
                "Usá «Convertir automáticamente» o escribila en SQL.")
    return None


def _aplicar_campo(c, kind: str, data: dict) -> None:
    c.name = data["name"].strip()
    c.expression = data["expression"].strip()
    c.description = (data.get("description") or "").strip() or None
    if kind == "dimension":
        c.data_type = data.get("data_type") or "string"
    else:
        c.format = data.get("format") or "number"


@app.get("/api/modelos/{nombre}/campos")
def api_listar_campos(nombre: str, db: Session = Depends(get_catalog_db)):
    modelo = _modelo_o_404(db, nombre)
    return JSONResponse({"ok": True, "campos": _campos_modelo(modelo)})


@app.post("/api/modelos/{nombre}/campos")
async def api_crear_campo(nombre: str, request: Request, db: Session = Depends(get_catalog_db)):
    data = await request.json()
    modelo = _modelo_o_404(db, nombre)
    kind = data.get("kind")
    if kind not in _KINDS:
        return JSONResponse({"ok": False, "error": "Tipo de campo inválido."})
    entity = next((e for e in modelo.entities if e.id == data.get("entity_id")), None)
    if entity is None:
        return JSONResponse({"ok": False, "error": "Entidad inválida."})
    error = _validar_campo(entity, kind, data)
    if error:
        return JSONResponse({"ok": False, "error": error})
    campo = _KINDS[kind](entity_id=entity.id)
    _aplicar_campo(campo, kind, data)
    db.add(campo)
    modelo.updated_at = _dt.utcnow()
    db.commit()
    db.refresh(campo)
    return JSONResponse({"ok": True, "campo": _campo_to_dict(campo, kind)})


def _campo_o_error(modelo: SemanticModel, kind: str, campo_id: int):
    if kind not in _KINDS:
        return None
    for e in modelo.entities:
        for c in (e.dimensions if kind == "dimension" else e.metrics):
            if c.id == campo_id:
                return e, c
    return None


@app.put("/api/modelos/{nombre}/campos/{kind}/{campo_id}")
async def api_editar_campo(nombre: str, kind: str, campo_id: int, request: Request,
                           db: Session = Depends(get_catalog_db)):
    data = await request.json()
    modelo = _modelo_o_404(db, nombre)
    found = _campo_o_error(modelo, kind, campo_id)
    if not found:
        return JSONResponse({"ok": False, "error": "Campo no encontrado."})
    entity, campo = found
    error = _validar_campo(entity, kind, data, actual=campo)
    if error:
        return JSONResponse({"ok": False, "error": error})
    # Modelos importados antes de tableau_formula: la fórmula original solo vive en
    # la expresión; se preserva antes de reemplazarla por SQL.
    if not campo.tableau_formula and es_sintaxis_tableau(campo.expression or ""):
        campo.tableau_formula = campo.expression
    _aplicar_campo(campo, kind, data)
    modelo.updated_at = _dt.utcnow()
    db.commit()
    db.refresh(campo)
    return JSONResponse({"ok": True, "campo": _campo_to_dict(campo, kind)})


@app.delete("/api/modelos/{nombre}/campos/{kind}/{campo_id}")
def api_eliminar_campo(nombre: str, kind: str, campo_id: int, db: Session = Depends(get_catalog_db)):
    modelo = _modelo_o_404(db, nombre)
    found = _campo_o_error(modelo, kind, campo_id)
    if not found:
        return JSONResponse({"ok": False, "error": "Campo no encontrado."})
    db.delete(found[1])
    modelo.updated_at = _dt.utcnow()
    db.commit()
    return JSONResponse({"ok": True})


@app.post("/api/modelos/{nombre}/probar")
async def api_probar_campo(nombre: str, request: Request, db: Session = Depends(get_catalog_db)):
    """Ejecuta la expresión (sin guardarla) con el mismo compilador que usa el MCP."""
    data = await request.json()
    _modelo_o_404(db, nombre)
    kind = data.get("kind")
    campo = (data.get("name") or "").strip() or "__prueba__"
    expr = (data.get("expression") or "").strip()
    if not expr:
        return JSONResponse({"ok": False, "error": "La expresión está vacía."})
    periodo = data.get("periodo") or None
    if periodo and not periodo.get("campo"):
        periodo = None
    filtros = {k: v for k, v in (data.get("filtros") or {}).items() if k and v not in (None, "")}

    if kind == "metric":
        metricas, dimensiones, limite = [campo], [], 1
        overrides = {"metricas": {campo: {"expression": expr}}}
    else:
        metrica = data.get("metrica")
        metricas, dimensiones, limite = ([metrica] if metrica else []), [campo], 20
        overrides = {"dimensiones": {campo: {"expression": expr,
                                             "data_type": data.get("data_type")}}}
    try:
        r = _consultar_dw(nombre, metricas, dimensiones, filtros=filtros, periodo=periodo,
                          limite=limite, overrides=overrides)
    except ConsultaError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "sql": getattr(e, "sql", None)})
    return JSONResponse(jsonable_encoder({"ok": True, **r}))


@app.post("/api/modelos/{nombre}/convertir")
async def api_convertir_formula(nombre: str, request: Request, db: Session = Depends(get_catalog_db)):
    """Convierte una fórmula Tableau a SQL resolviendo referencias contra los campos del modelo."""
    data = await request.json()
    modelo = _modelo_o_404(db, nombre)
    formula = (data.get("formula") or "").strip()
    if not formula:
        return JSONResponse({"ok": False, "error": "No hay fórmula de Tableau para convertir."})
    excluir = (data.get("kind"), data.get("name"))
    campos: dict[str, dict] = {}
    for c in _campos_modelo(modelo):
        # El campo que se convierte no se resuelve a sí mismo: [X] dentro de X es la columna X
        if (c["kind"], c["name"]) != excluir:
            campos.setdefault(c["name"], c)
    try:
        sql = translate_formula(formula, make_field_resolver(campos, es_sintaxis_tableau))
    except NoTraducible as e:
        return JSONResponse({"ok": False, "error": f"No se puede convertir automáticamente: {e}"})
    return JSONResponse({"ok": True, "sql": sql})


@app.get("/api/modelos/{nombre}/columnas")
def api_columnas_modelo(nombre: str, db: Session = Depends(get_catalog_db)):
    """Columnas del DW de las tablas del modelo (autocompletado del editor)."""
    modelo = _modelo_o_404(db, nombre)
    tablas, base = [], None
    for e in modelo.entities:
        base = base or e.base_table
        tablas += [e.base_table] + [j.get("table") for j in (e.joins or [])]
    try:
        return JSONResponse({"ok": True, "base_table": base, "tablas": columnas_tabla(tablas)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/modelos/{nombre}/validar")
def api_validar_modelo(nombre: str, db: Session = Depends(get_catalog_db)):
    """Compila todos los campos contra el DW (sin traer datos) y devuelve los errores."""
    _modelo_o_404(db, nombre)
    t0 = _dt.utcnow()
    try:
        resultados = validar_campos(nombre)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return JSONResponse({
        "ok": True,
        "resultados": resultados,
        "tiempo_ms": int((_dt.utcnow() - t0).total_seconds() * 1000),
    })


@app.put("/api/modelos/{nombre}")
async def api_editar_cabecera_modelo(nombre: str, request: Request, db: Session = Depends(get_catalog_db)):
    data = await request.json()
    modelo = _modelo_o_404(db, nombre)
    nuevo = (data.get("name") or "").strip()
    if not nuevo:
        return JSONResponse({"ok": False, "error": "El nombre es obligatorio."})
    if nuevo != nombre and db.query(SemanticModel).filter(SemanticModel.name == nuevo).first():
        return JSONResponse({"ok": False, "error": f"Ya existe un modelo '{nuevo}'."})
    modelo.name = nuevo
    modelo.description = (data.get("description") or "").strip() or None
    modelo.updated_at = _dt.utcnow()
    db.commit()
    return JSONResponse({"ok": True, "name": modelo.name})


def _datos_entidad(data: dict) -> tuple[dict | None, str | None]:
    name = (data.get("name") or "").strip()
    base = (data.get("base_table") or "").strip()
    if not name or not base:
        return None, "Nombre y tabla base son obligatorios."
    joins = []
    for j in data.get("joins") or []:
        table = (j.get("table") or "").strip()
        if not table:
            continue
        tipo = (j.get("type") or "LEFT").upper()
        if tipo not in _JOIN_TYPES:
            return None, f"Tipo de join inválido: {tipo}"
        joins.append({"table": table, "alias": (j.get("alias") or "").strip() or table,
                      "on": (j.get("on") or "").strip(), "type": tipo})
    return {"name": name, "description": (data.get("description") or "").strip() or None,
            "base_table": base, "joins": joins}, None


@app.post("/api/modelos/{nombre}/entidades")
async def api_crear_entidad(nombre: str, request: Request, db: Session = Depends(get_catalog_db)):
    modelo = _modelo_o_404(db, nombre)
    datos, error = _datos_entidad(await request.json())
    if error:
        return JSONResponse({"ok": False, "error": error})
    entity = SemanticEntity(model_id=modelo.id, **datos)
    db.add(entity)
    modelo.updated_at = _dt.utcnow()
    db.commit()
    db.refresh(entity)
    return JSONResponse({"ok": True, "entidad": _entidad_to_dict(entity)})


@app.put("/api/modelos/{nombre}/entidades/{entity_id}")
async def api_editar_entidad(nombre: str, entity_id: int, request: Request,
                             db: Session = Depends(get_catalog_db)):
    modelo = _modelo_o_404(db, nombre)
    entity = next((e for e in modelo.entities if e.id == entity_id), None)
    if entity is None:
        return JSONResponse({"ok": False, "error": "Entidad no encontrada."})
    datos, error = _datos_entidad(await request.json())
    if error:
        return JSONResponse({"ok": False, "error": error})
    for k, v in datos.items():
        setattr(entity, k, v)
    modelo.updated_at = _dt.utcnow()
    db.commit()
    return JSONResponse({"ok": True, "entidad": _entidad_to_dict(entity)})


@app.delete("/api/modelos/{nombre}/entidades/{entity_id}")
def api_eliminar_entidad(nombre: str, entity_id: int, db: Session = Depends(get_catalog_db)):
    modelo = _modelo_o_404(db, nombre)
    entity = next((e for e in modelo.entities if e.id == entity_id), None)
    if entity is None:
        return JSONResponse({"ok": False, "error": "Entidad no encontrada."})
    db.delete(entity)
    modelo.updated_at = _dt.utcnow()
    db.commit()
    return JSONResponse({"ok": True})


@app.get("/api/modelos/{nombre}/yaml")
def api_exportar_yaml(nombre: str, db: Session = Depends(get_catalog_db)):
    from fastapi import HTTPException
    modelo = db.query(SemanticModel).filter(SemanticModel.name == nombre).first()
    if not modelo:
        raise HTTPException(status_code=404, detail="Modelo no encontrado.")
    content = _yaml.dump(
        _modelo_to_dict(modelo),
        allow_unicode=True, default_flow_style=False, sort_keys=False
    )
    return Response(
        content=content,
        media_type="text/yaml",
        headers={"Content-Disposition": f"attachment; filename={nombre}.yaml"},
    )


@app.post("/api/modelos/importar-twb")
async def api_importar_twb(file: UploadFile = File(...), db: Session = Depends(get_catalog_db)):
    from src.catalog.twb_parser import parse_twb

    content = await file.read()
    try:
        datasources = parse_twb(content)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})

    created: list[str] = []
    skipped: list[str] = []
    pendientes: dict[str, list[dict]] = {}
    avisos: dict[str, list[str]] = {}

    for ds in datasources:
        if db.query(SemanticModel).filter(SemanticModel.name == ds.model_name).first():
            skipped.append(ds.model_name)
            continue

        modelo = SemanticModel(
            name=ds.model_name,
            description=ds.description,
            updated_at=_dt.utcnow(),
        )
        db.add(modelo)
        db.flush()

        joins = [{"table": t, "on": "", "type": "LEFT"} for t in ds.join_tables]
        entity = SemanticEntity(
            model_id=modelo.id,
            name=ds.caption,
            description=ds.description,
            base_table=ds.base_table,
            joins=joins,
        )
        db.add(entity)
        db.flush()

        for dim in ds.dimensions:
            db.add(SemanticDimension(
                entity_id=entity.id,
                name=dim["name"],
                description=dim["description"] or None,
                expression=dim["expression"],
                data_type=dim["data_type"] or "string",
                tableau_formula=dim.get("tableau_formula"),
            ))

        for met in ds.metrics:
            db.add(SemanticMetric(
                entity_id=entity.id,
                name=met["name"],
                description=met["description"] or None,
                expression=met["expression"],
                format=met["format"] or "number",
                tableau_formula=met.get("tableau_formula"),
            ))

        db.commit()
        created.append(ds.model_name)
        if ds.pendientes:
            pendientes[ds.model_name] = ds.pendientes
        if ds.avisos:
            avisos[ds.model_name] = ds.avisos

    return JSONResponse({
        "ok": bool(created), "created": created, "skipped": skipped,
        "pendientes": pendientes, "avisos": avisos,
    })
