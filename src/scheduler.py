import logging
import uuid
from datetime import date, datetime, time as dt_time
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from src.config import load_flows, load_settings, settings
from src.runner import ejecutar_flow

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=settings["timezone"])


def _disparar_scheduled(nombre: str, archivo: str, credenciales=None, reintentos: int = 0, reintento_espera_min: int = 5) -> None:
    ejecutar_flow(
        nombre=nombre,
        archivo=archivo,
        credenciales=credenciales,
        disparador="scheduler",
        grupo_id=str(uuid.uuid4()),
        reintentos=reintentos,
        reintento_espera_min=reintento_espera_min,
    )


def _registrar_flows() -> int:
    flows = load_flows()
    jobs_registrados = 0
    for flow in flows:
        if not flow.get("enabled", True):
            logger.info(f"[{flow['name']}] Deshabilitado, se omite.")
            continue

        schedules = flow.get("schedules") or []
        deps = flow.get("depends_on") or []

        if not schedules and not deps:
            logger.warning(f"[{flow['name']}] Sin schedule ni depends_on — nunca se disparará.")
            continue

        if not schedules:
            logger.info(f"[{flow['name']}] Flow pasivo (solo por dependencia).")
            continue

        for i, schedule in enumerate(schedules):
            job_id = f"{flow['name']}__s{i}"
            scheduler.add_job(
                _disparar_scheduled,
                trigger=CronTrigger.from_crontab(schedule, timezone=settings["timezone"]),
                kwargs={
                    "nombre": flow["name"],
                    "archivo": flow["file"],
                    "credenciales": flow.get("credentials"),
                    "reintentos": flow.get("reintentos", 0),
                    "reintento_espera_min": flow.get("reintento_espera_min", 5),
                },
                id=job_id,
                name=f"{flow['name']} [{schedule}]",
                replace_existing=True,
                misfire_grace_time=300,
            )
            logger.info(f"[{flow['name']}] Disparador #{i+1}: '{schedule}'")
            jobs_registrados += 1

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
    n = _registrar_flows()
    _registrar_resumen()
    scheduler.start()
    logger.info(f"Scheduler iniciado — {n} disparador(es) activos.")


def recargar_scheduler() -> None:
    scheduler.remove_all_jobs()
    n = _registrar_flows()
    _registrar_resumen()
    logger.info(f"Scheduler recargado — {n} disparador(es) activos.")


def detener_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler detenido.")
