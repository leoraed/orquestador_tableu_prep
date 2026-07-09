import logging
import subprocess
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from src.config import load_settings, load_flows, BASE_DIR
from src.database import SessionLocal
from src.models import EjecucionFlow

logger = logging.getLogger(__name__)


def _disparar_dependientes(nombre_completado: str, grupo_id: str) -> None:
    s = load_settings()
    ttl = s.get("ttl_grupo_horas", 2)
    flows = load_flows()
    ventana_inicio = datetime.utcnow() - timedelta(hours=ttl)

    for flow in flows:
        deps = flow.get("depends_on") or []
        if nombre_completado not in deps or not flow.get("enabled", True):
            continue

        db = SessionLocal()
        try:
            # Buscar la ejecución exitosa más reciente de cada dep dentro de la ventana TTL
            t_exitos = []
            todas_ok = True
            for dep in deps:
                ej = (
                    db.query(EjecucionFlow)
                    .filter(
                        EjecucionFlow.nombre_flow == dep,
                        EjecucionFlow.estado == "exitoso",
                        EjecucionFlow.inicio >= ventana_inicio,
                    )
                    .order_by(EjecucionFlow.inicio.desc())
                    .first()
                )
                if not ej:
                    todas_ok = False
                    break
                t_exitos.append(ej.inicio)

            if not todas_ok:
                logger.debug(f"[{flow['name']}] Dependencias aún no satisfechas — esperando.")
                continue

            # Evitar doble disparo: omitir si ya se disparó por dependencia
            # después del éxito más antiguo de este batch
            t_ref = min(t_exitos)
            ya_disparado = db.query(EjecucionFlow).filter(
                EjecucionFlow.nombre_flow == flow["name"],
                EjecucionFlow.inicio >= t_ref,
                EjecucionFlow.disparador == "dependencia",
            ).first()

            if ya_disparado:
                logger.debug(f"[{flow['name']}] Ya fue disparado en esta ventana — omitiendo.")
                continue

            logger.info(f"[{flow['name']}] Todas las dependencias satisfechas → disparando.")
        finally:
            db.close()

        threading.Thread(
            target=ejecutar_flow,
            kwargs={
                "nombre": flow["name"],
                "archivo": flow["file"],
                "credenciales": flow.get("credentials"),
                "disparador": "dependencia",
                "grupo_id": grupo_id,
            },
            daemon=True,
        ).start()


def ejecutar_flow(
    nombre: str,
    archivo: str,
    credenciales: str | None = None,
    disparador: str = "scheduler",
    grupo_id: str | None = None,
) -> str:
    if grupo_id is None:
        grupo_id = str(uuid.uuid4())

    db = SessionLocal()
    ejecucion = EjecucionFlow(
        nombre_flow=nombre,
        archivo_flow=archivo,
        inicio=datetime.utcnow(),
        estado="en_proceso",
        disparador=disparador,
        grupo_id=grupo_id,
    )
    db.add(ejecucion)
    db.commit()
    db.refresh(ejecucion)

    s = load_settings()
    archivo_abs = str(BASE_DIR / archivo) if not Path(archivo).is_absolute() else archivo
    cli = s["prep_cli_path"]
    timeout = s.get("timeout_segundos", 3600)

    cmd = ["cmd", "/c", cli, "-t", archivo_abs]
    if credenciales:
        cred_abs = str(BASE_DIR / credenciales) if not Path(credenciales).is_absolute() else credenciales
        cmd += ["-c", cred_abs]

    logger.info(f"[{nombre}] Iniciando (grupo {grupo_id[:8]}): {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        ejecucion.fin = datetime.utcnow()
        ejecucion.salida = result.stdout

        if result.returncode == 0:
            ejecucion.estado = "exitoso"
            logger.info(f"[{nombre}] Completado exitosamente (grupo {grupo_id[:8]}).")
        else:
            ejecucion.estado = "fallido"
            ejecucion.error = result.stderr
            logger.error(f"[{nombre}] Falló — código {result.returncode} (grupo {grupo_id[:8]}).")

    except subprocess.TimeoutExpired:
        ejecucion.estado = "fallido"
        ejecucion.error = f"Timeout: superó {timeout}s."
        logger.error(f"[{nombre}] Timeout (grupo {grupo_id[:8]}).")

    except Exception as exc:
        ejecucion.estado = "fallido"
        ejecucion.error = str(exc)
        logger.exception(f"[{nombre}] Error inesperado (grupo {grupo_id[:8]}): {exc}")

    finally:
        db.commit()
        db.close()

    if ejecucion.estado == "exitoso":
        _disparar_dependientes(nombre, grupo_id)

    return ejecucion.estado
