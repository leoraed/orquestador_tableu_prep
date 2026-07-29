import logging
import threading
import uuid
from datetime import date, datetime, time as dt_time
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from src.config import load_pipelines, load_flows, load_settings, settings
from src.runner import ejecutar_flow

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=settings["timezone"])


def _disparar_pipeline(pipeline_name: str) -> None:
    pipelines = {p["name"]: p for p in load_pipelines()}
    pipeline = pipelines.get(pipeline_name)
    if not pipeline:
        logger.warning(f"Pipeline '{pipeline_name}' no encontrado en la configuración.")
        return

    flows_map = {f["name"]: f for f in load_flows()}
    grupo_id = str(uuid.uuid4())

    root_steps = [s for s in pipeline.get("flows", []) if not s.get("depends_on")]

    if not root_steps:
        logger.warning(f"[{pipeline_name}] Sin flows raíz (sin depends_on) — pipeline no iniciará.")
        return

    logger.info(f"[{pipeline_name}] Disparando (grupo {grupo_id[:8]}) — {len(root_steps)} flow(s) raíz.")

    for step in root_steps:
        flow = flows_map.get(step["flow"])
        if not flow:
            logger.warning(f"[{pipeline_name}] Flow '{step['flow']}' no encontrado — omitido.")
            continue
        if not flow.get("enabled", True):
            logger.info(f"[{pipeline_name}] Flow '{step['flow']}' deshabilitado — omitido.")
            continue
        threading.Thread(
            target=ejecutar_flow,
            kwargs={
                "nombre": flow["name"],
                "archivo": flow["file"],
                "credenciales": flow.get("credentials"),
                "disparador": "scheduler",
                "grupo_id": grupo_id,
                "reintentos": flow.get("reintentos", 0),
                "reintento_espera_min": flow.get("reintento_espera_min", 5),
                "pipeline_name": pipeline_name,
            },
            daemon=True,
        ).start()


def _registrar_pipelines() -> int:
    pipelines = load_pipelines()
    jobs_registrados = 0

    for pipeline in pipelines:
        nombre = pipeline["name"]
        schedules = pipeline.get("schedules") or []

        if not schedules:
            logger.info(f"[{nombre}] Pipeline sin schedule — solo disparo manual.")
            continue

        for i, schedule in enumerate(schedules):
            job_id = f"{nombre}__s{i}"
            try:
                scheduler.add_job(
                    _disparar_pipeline,
                    trigger=CronTrigger.from_crontab(schedule, timezone=settings["timezone"]),
                    kwargs={"pipeline_name": nombre},
                    id=job_id,
                    name=f"{nombre} [{schedule}]",
                    replace_existing=True,
                    misfire_grace_time=300,
                )
                logger.info(f"[{nombre}] Disparador #{i+1}: '{schedule}'")
                jobs_registrados += 1
            except Exception as exc:
                logger.error(f"[{nombre}] Cron inválido '{schedule}': {exc}")

    return jobs_registrados


def _enviar_resumen_diario() -> None:
    from src.database import SessionLocal
    from src.models import EjecucionFlow
    from src.notificaciones import enviar_telegram

    db = SessionLocal()
    try:
        inicio_hoy = datetime.combine(date.today(), dt_time.min)
        ejecuciones = db.query(EjecucionFlow).filter(EjecucionFlow.inicio >= inicio_hoy).all()

        total = len(ejecuciones)
        if total == 0:
            mensaje = f"Resumen diario {date.today().strftime('%d/%m/%Y')}: sin ejecuciones."
        else:
            exitosas  = sum(1 for e in ejecuciones if e.estado == "exitoso")
            fallidas  = sum(1 for e in ejecuciones if e.estado == "fallido")
            canceladas = sum(1 for e in ejecuciones if e.estado == "cancelado")
            en_proceso = sum(1 for e in ejecuciones if e.estado == "en_proceso")

            lineas = [f"Resumen diario — {date.today().strftime('%d/%m/%Y')}"]
            lineas.append(f"Total: {total}  |  OK: {exitosas}  |  Fallidas: {fallidas}")
            if canceladas:
                lineas.append(f"Canceladas: {canceladas}")
            if en_proceso:
                lineas.append(f"En proceso: {en_proceso}")
            if fallidas:
                nombres = [e.nombre_flow for e in ejecuciones if e.estado == "fallido"]
                lineas.append("Flows con fallo: " + ", ".join(sorted(set(nombres))))
            mensaje = "\n".join(lineas)

        enviar_telegram(mensaje)
        logger.info("Resumen diario enviado por Telegram.")
    except Exception as exc:
        logger.error(f"Error generando resumen diario: {exc}")
    finally:
        db.close()


def _registrar_resumen() -> None:
    s = load_settings()
    cron = s.get("telegram_resumen_cron", "").strip()
    if not cron:
        return
    try:
        scheduler.add_job(
            _enviar_resumen_diario,
            trigger=CronTrigger.from_crontab(cron, timezone=settings["timezone"]),
            id="__resumen_diario__",
            name="Resumen diario Telegram",
            replace_existing=True,
            misfire_grace_time=600,
        )
        logger.info(f"Resumen diario Telegram programado: '{cron}'")
    except Exception as exc:
        logger.error(f"Cron de resumen inválido '{cron}': {exc}")


def inicializar_scheduler() -> None:
    n = _registrar_pipelines()
    _registrar_resumen()
    scheduler.start()
    logger.info(f"Scheduler iniciado — {n} disparador(es) activos.")


def recargar_scheduler() -> None:
    scheduler.remove_all_jobs()
    n = _registrar_pipelines()
    _registrar_resumen()
    logger.info(f"Scheduler recargado — {n} disparador(es) activos.")


def detener_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler detenido.")
