from contextlib import asynccontextmanager
from datetime import datetime, date, time as dt_time
from typing import Optional
from pathlib import Path
from urllib.parse import quote
import yaml

import uuid
from typing import List
from fastapi import FastAPI, Depends, BackgroundTasks, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from src.database import get_db
from src.models import EjecucionFlow
from src.runner import ejecutar_flow
from src.config import load_flows, load_settings, load_carpetas, descubrir_tfl, BASE_DIR, _load_yaml, _save_yaml
from src.scheduler import inicializar_scheduler, detener_scheduler, recargar_scheduler, scheduler

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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


app = FastAPI(title="Orquestador Tableau Prep", version="1.0.0", lifespan=lifespan)


# ── helpers ────────────────────────────────────────────────────────────────

def _redir(path: str, msg: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?msg={quote(msg)}", status_code=303)


def _guardar_flows(flows: list[dict]) -> None:
    data = _load_yaml()
    data["flows"] = flows
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
    """Mapea flow_name → próxima ejecución más cercana entre todos sus schedules."""
    result: dict[str, str] = {}
    for job in scheduler.get_jobs():
        flow_name = job.id.rsplit("__s", 1)[0] if "__s" in job.id else job.id
        if job.next_run_time:
            nrt = str(job.next_run_time)
            if flow_name not in result or nrt < result[flow_name]:
                result[flow_name] = nrt
    return result


# ── páginas web ────────────────────────────────────────────────────────────

@app.get("/")
def dashboard(request: Request, msg: str = None, db: Session = Depends(get_db)):
    flows = load_flows()
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
    carpetas = load_carpetas()
    descubiertos = descubrir_tfl(carpetas)
    paths_registrados = {f["file"] for f in flows}
    todos_nombres = [f["name"] for f in flows]
    return templates.TemplateResponse("flows.html", {
        "request": request,
        "flows": flows,
        "jobs": _jobs_map(),
        "carpetas": carpetas,
        "descubiertos": descubiertos,
        "paths_registrados": paths_registrados,
        "todos_nombres": todos_nombres,
        "mensaje": msg,
        "active": "flows",
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
    schedules: List[str] = Form(default=[]),
    enabled: Optional[str] = Form(None),
    credentials: Optional[str] = Form(None),
    depends_on: List[str] = Form(default=[]),
):
    flows = load_flows()
    if any(f["name"] == name.strip() for f in flows):
        return _redir("/flows", f"Error: ya existe un flow '{name}'.")
    flows.append({
        "name": name.strip(),
        "file": file.strip(),
        "schedules": [s.strip() for s in schedules if s.strip()],
        "enabled": enabled is not None,
        "credentials": credentials.strip() if credentials and credentials.strip() else None,
        "depends_on": depends_on,
    })
    _guardar_flows(flows)
    recargar_scheduler()
    return _redir("/flows", f"Flow '{name}' creado exitosamente.")


@app.post("/api/flows/{nombre}/editar")
def api_editar_flow(
    nombre: str,
    name: str = Form(...),
    file: str = Form(...),
    schedules: List[str] = Form(default=[]),
    enabled: Optional[str] = Form(None),
    credentials: Optional[str] = Form(None),
    depends_on: List[str] = Form(default=[]),
):
    flows = load_flows()
    idx = next((i for i, f in enumerate(flows) if f["name"] == nombre), None)
    if idx is None:
        return _redir("/flows", f"Error: flow '{nombre}' no encontrado.")
    flows[idx] = {
        "name": name.strip(),
        "file": file.strip(),
        "schedules": [s.strip() for s in schedules if s.strip()],
        "enabled": enabled is not None,
        "credentials": credentials.strip() if credentials and credentials.strip() else None,
        "depends_on": depends_on,
    }
    _guardar_flows(flows)
    recargar_scheduler()
    return _redir("/flows", f"Flow '{name}' actualizado.")


@app.post("/api/flows/{nombre}/eliminar")
def api_eliminar_flow(nombre: str):
    flows = load_flows()
    nuevos = [f for f in flows if f["name"] != nombre]
    if len(nuevos) == len(flows):
        return _redir("/flows", f"Error: flow '{nombre}' no encontrado.")
    _guardar_flows(nuevos)
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
    )
    return _redir("/", f"Flow '{nombre}' iniciado manualmente.")


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
):
    s = load_settings()
    s["prep_cli_path"] = prep_cli_path.strip()
    s["timezone"] = timezone.strip()
    s["timeout_segundos"] = timeout_segundos
    s["database_url"] = database_url.strip()
    s["ttl_grupo_horas"] = ttl_grupo_horas
    _guardar_settings(s)
    return _redir("/configuracion", "Configuración guardada. Reiniciá el servidor para aplicar cambios de DB o timezone.")


# ── API JSON ───────────────────────────────────────────────────────────────

@app.get("/api/ejecuciones")
def api_ejecuciones(limit: int = 50, db: Session = Depends(get_db)):
    return db.query(EjecucionFlow).order_by(EjecucionFlow.inicio.desc()).limit(limit).all()


@app.get("/health")
def health():
    return {"estado": "ok", "timestamp": datetime.utcnow().isoformat()}
