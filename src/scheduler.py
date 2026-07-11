import logging
import uuid
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from src.config import load_flows, settings
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


def inicializar_scheduler() -> None:
    n = _registrar_flows()
    scheduler.start()
    logger.info(f"Scheduler iniciado — {n} disparador(es) activos.")


def recargar_scheduler() -> None:
    scheduler.remove_all_jobs()
    n = _registrar_flows()
    logger.info(f"Scheduler recargado — {n} disparador(es) activos.")


def detener_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler detenido.")
