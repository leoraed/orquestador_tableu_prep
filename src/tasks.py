import logging
import threading
import uuid
from datetime import datetime

from src.config import load_settings
from src.database import SessionLocal
from src.models import EjecucionFlow

logger = logging.getLogger(__name__)


def _correr_tableau_cloud(
    nombre: str,
    resource_type: str,
    resource_name: str,
    disparador: str,
    grupo_id: str,
    pipeline_name: str | None = None,
) -> str:
    try:
        import tableauserverclient as TSC
    except ImportError:
        raise RuntimeError("tableauserverclient no instalado. Ejecutá: pip install tableauserverclient")

    s = load_settings()
    tc = s.get("tableau_cloud", {}) or {}

    db = SessionLocal()
    ejecucion = EjecucionFlow(
        nombre_flow=nombre,
        archivo_flow=f"tableau_cloud:{resource_type}:{resource_name}",
        inicio=datetime.utcnow(),
        estado="en_proceso",
        disparador=disparador,
        grupo_id=grupo_id,
        pipeline_name=pipeline_name,
        step_type="tableau_cloud",
    )
    db.add(ejecucion)
    db.commit()
    db.refresh(ejecucion)

    prefix = f"[{nombre}]" + (f" [{pipeline_name}]" if pipeline_name else "")
    logger.info(f"{prefix} Iniciando Tableau Cloud refresh [{disparador}]: {resource_type}={resource_name}")

    try:
        auth = TSC.PersonalAccessTokenAuth(
            token_name=tc["token_name"],
            personal_access_token=tc["token_value"],
            site_id=tc["site"],
        )
        server = TSC.Server(tc["server"], use_server_version=True)

        with server.auth.sign_in(auth):
            if resource_type == "datasource":
                all_ds, _ = server.datasources.get()
                target = next((d for d in all_ds if d.name == resource_name), None)
                if not target:
                    raise ValueError(f"Datasource '{resource_name}' no encontrado en Tableau Cloud")
                job = server.datasources.refresh(target)
            elif resource_type == "workbook":
                all_wb, _ = server.workbooks.get()
                target = next((w for w in all_wb if w.name == resource_name), None)
                if not target:
                    raise ValueError(f"Workbook '{resource_name}' no encontrado en Tableau Cloud")
                job = server.workbooks.refresh(target)
            else:
                raise ValueError(f"Tipo de recurso desconocido: {resource_type}")

        salida = f"Refresh encolado en Tableau Cloud.\nJob ID: {job.id}\nStatus inicial: {job.status}"
        ejecucion.fin = datetime.utcnow()
        ejecucion.estado = "exitoso"
        ejecucion.salida = salida
        logger.info(f"{prefix} Refresh encolado. Job ID: {job.id}")

    except Exception as exc:
        ejecucion.fin = datetime.utcnow()
        ejecucion.estado = "fallido"
        ejecucion.error = str(exc)
        logger.error(f"{prefix} Error al disparar refresh: {exc}")
    finally:
        db.commit()
        db.close()

    return ejecucion.estado


def ejecutar_task(
    task: dict,
    disparador: str = "manual",
    grupo_id: str | None = None,
    pipeline_name: str | None = None,
) -> str:
    if grupo_id is None:
        grupo_id = str(uuid.uuid4())

    tipo = task.get("type", "")
    nombre = task["name"]

    if tipo == "tableau_cloud":
        estado = _correr_tableau_cloud(
            nombre=nombre,
            resource_type=task.get("resource_type", "workbook"),
            resource_name=task["resource_name"],
            disparador=disparador,
            grupo_id=grupo_id,
            pipeline_name=pipeline_name,
        )
    else:
        raise ValueError(f"Tipo de tarea desconocido: {tipo}")

    if estado == "exitoso" and pipeline_name:
        from src.runner import _disparar_dependientes_en_pipeline
        _disparar_dependientes_en_pipeline(nombre, grupo_id, pipeline_name)

    return estado
