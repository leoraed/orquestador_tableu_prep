import logging
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from src.config import load_settings, load_flows, BASE_DIR
from src.database import SessionLocal
from src.models import EjecucionFlow

logger = logging.getLogger(__name__)

# ejecucion.id -> Popen (para poder cancelar)
_procesos_activos: dict[int, subprocess.Popen] = {}
_cancelados: set[int] = set()
_lock = threading.Lock()


def _matar_proceso(proc: subprocess.Popen) -> None:
    """Mata el proceso y todos sus hijos (necesario en Windows para cmd /c .bat)."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    except Exception:
        proc.kill()


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
                "reintentos": flow.get("reintentos", 0),
                "reintento_espera_min": flow.get("reintento_espera_min", 5),
            },
            daemon=True,
        ).start()


def _correr_subprocess(
    nombre: str,
    archivo: str,
    credenciales: str | None,
    disparador: str,
    grupo_id: str,
) -> str:
    s = load_settings()
    archivo_abs = str(BASE_DIR / archivo) if not Path(archivo).is_absolute() else archivo
    cli = s["prep_cli_path"]
    timeout = s.get("timeout_segundos", 3600)

    cmd = ["cmd", "/c", cli, "-t", archivo_abs]
    if credenciales:
        cred_abs = str(BASE_DIR / credenciales) if not Path(credenciales).is_absolute() else credenciales
        cmd += ["-c", cred_abs]

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
    eid = ejecucion.id

    logger.info(f"[{nombre}] Iniciando [{disparador}] (grupo {grupo_id[:8]}): {' '.join(cmd)}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        with _lock:
            _procesos_activos[eid] = proc

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _matar_proceso(proc)
            proc.communicate()
            raise

        ejecucion.fin = datetime.utcnow()

        with _lock:
            fue_cancelado = eid in _cancelados
            _cancelados.discard(eid)

        if fue_cancelado:
            ejecucion.estado = "cancelado"
            ejecucion.error = "Cancelado manualmente."
            logger.warning(f"[{nombre}] Cancelado (grupo {grupo_id[:8]}).")
        elif proc.returncode == 0:
            ejecucion.estado = "exitoso"
            ejecucion.salida = stdout
            logger.info(f"[{nombre}] Completado exitosamente (grupo {grupo_id[:8]}).")
        else:
            ejecucion.estado = "fallido"
            ejecucion.salida = stdout
            ejecucion.error = stderr
            logger.error(f"[{nombre}] Falló — código {proc.returncode} (grupo {grupo_id[:8]}).")

    except subprocess.TimeoutExpired:
        ejecucion.fin = datetime.utcnow()
        ejecucion.estado = "fallido"
        ejecucion.error = f"Timeout: superó {timeout}s."
        logger.error(f"[{nombre}] Timeout (grupo {grupo_id[:8]}).")
    except Exception as exc:
        ejecucion.fin = datetime.utcnow()
        ejecucion.estado = "fallido"
        ejecucion.error = str(exc)
        logger.exception(f"[{nombre}] Error inesperado (grupo {grupo_id[:8]}): {exc}")
    finally:
        with _lock:
            _procesos_activos.pop(eid, None)
        db.commit()
        db.close()

    return ejecucion.estado


def ejecutar_flow(
    nombre: str,
    archivo: str,
    credenciales: str | None = None,
    disparador: str = "scheduler",
    grupo_id: str | None = None,
    reintentos: int = 0,
    reintento_espera_min: int = 5,
) -> str:
    if grupo_id is None:
        grupo_id = str(uuid.uuid4())

    max_intentos = reintentos + 1
    estado = "fallido"

    for intento in range(1, max_intentos + 1):
        disp = disparador if intento == 1 else f"reintento_{intento}/{max_intentos}"
        estado = _correr_subprocess(nombre, archivo, credenciales, disp, grupo_id)

        if estado in ("exitoso", "cancelado"):
            break

        if intento < max_intentos:
            logger.warning(
                f"[{nombre}] Fallo en intento {intento}/{max_intentos} — "
                f"reintentando en {reintento_espera_min} min."
            )
            time.sleep(reintento_espera_min * 60)

    if estado == "exitoso":
        _disparar_dependientes(nombre, grupo_id)

    return estado
