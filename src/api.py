import logging
from contextlib import asynccontextmanager
from datetime import datetime, date, time as dt_time
from typing import Optional
from pathlib import Path
from urllib.parse import quote
import yaml

import uuid
from typing import List
from fastapi import FastAPI, Depends, BackgroundTasks, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from src.database import get_db
from src.models import EjecucionFlow
from src.runner import ejecutar_flow, _procesos_activos, _cancelados, _lock, _matar_proceso
from src.config import load_flows, load_pipelines, load_settings, load_carpetas, descubrir_tfl, BASE_DIR, _load_yaml, _save_yaml
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
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
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
    return templates.TemplateResponse("flows.html", {
        "request": request,
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
    pipelines = load_pipelines()

    flows_en_grafo = flows
    deps_en_pipeline: dict[str, list[str]] = {}
    pipeline_seleccionado = None

    if pipeline:
        pip = next((p for p in pipelines if p["name"] == pipeline), None)
        if pip:
            pipeline_seleccionado = pip["name"]
            nombres = {s["flow"] for s in pip.get("flows", [])}
            flows_en_grafo = [f for f in flows if f["name"] in nombres]
            for step in pip.get("flows", []):
                deps_en_pipeline[step["flow"]] = step.get("depends_on", [])

    ultimos_estados = {}
    for flow in flows_en_grafo:
        ej = db.query(EjecucionFlow).filter(
            EjecucionFlow.nombre_flow == flow["name"]
        ).order_by(EjecucionFlow.inicio.desc()).first()
        info = _info_ejecucion(ej)
        if info:
            ultimos_estados[flow["name"]] = info

    return templates.TemplateResponse("grafo.html", {
        "request": request,
        "flows": flows_en_grafo,
        "deps_en_pipeline": deps_en_pipeline,
        "pipeline_seleccionado": pipeline_seleccionado,
        "pipelines": pipelines,
        "ultimos_estados": ultimos_estados,
        "active": "grafo",
    })


@app.get("/api/grafo/estados")
def api_grafo_estados(pipeline: str = None, db: Session = Depends(get_db)):
    flows = load_flows()
    if pipeline:
        pips = {p["name"]: p for p in load_pipelines()}
        pip = pips.get(pipeline)
        if pip:
            nombres = {s["flow"] for s in pip.get("flows", [])}
            flows = [f for f in flows if f["name"] in nombres]
    result = {}
    for flow in flows:
        ej = db.query(EjecucionFlow).filter(
            EjecucionFlow.nombre_flow == flow["name"]
        ).order_by(EjecucionFlow.inicio.desc()).first()
        result[flow["name"]] = _info_ejecucion(ej)
    return result


@app.get("/pipelines")
def pagina_pipelines(request: Request, msg: str = None):
    flows = load_flows()
    pipelines = load_pipelines()
    jobs = _jobs_map()
    return templates.TemplateResponse("pipelines.html", {
        "request": request,
        "flows": flows,
        "pipelines": pipelines,
        "jobs": jobs,
        "mensaje": msg,
        "active": "pipelines",
    })


@app.get("/configuracion")
def pagina_configuracion(request: Request, msg: str = None):
    return templates.TemplateResponse("configuracion.html", {
        "request": request,
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
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "lineas": contenido,
        "lineas_n": lineas,
        "active": "logs",
    })


@app.get("/historial")
def pagina_historial(request: Request, flow: str = None, db: Session = Depends(get_db)):
    q = db.query(EjecucionFlow).order_by(EjecucionFlow.inicio.desc())
    if flow:
        q = q.filter(EjecucionFlow.nombre_flow == flow)
    return templates.TemplateResponse("historial.html", {
        "request": request,
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
