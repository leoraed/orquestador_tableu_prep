import logging
import os
import subprocess
import tempfile
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

    cred_abs = None
    if credenciales:
        cred_abs = str(BASE_DIR / credenciales) if not Path(credenciales).is_absolute() else credenciales

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

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".log", prefix="tprep_")
    os.close(tmp_fd)

    # list2cmdline escapa correctamente los paths con espacios para cmd.exe
    # shell=True invoca cmd /c y permite que interprete > y 2>&1 nativamente
    args = [cli, "-t", archivo_abs]
    if cred_abs:
        args += ["-c", cred_abs]
    cmd_shell = subprocess.list2cmdline(args) + f' > "{tmp_path}" 2>&1'

    logger.info(f"[{nombre}] Iniciando [{disparador}] (grupo {grupo_id[:8]}): {cmd_shell}")

    try:
        proc = subprocess.Popen(
            cmd_shell,
            shell=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        with _lock:
            _procesos_activos[eid] = proc

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _matar_proceso(proc)
            proc.wait()
            raise

        ejecucion.fin = datetime.utcnow()

        with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
            output = f.read().strip() or "(sin salida)"
        ejecucion.salida = output

        # Volcar output del CLI al log de Python para que aparezca en /logs
        for line in output.splitlines():
            if line.strip():
                logger.info(f"  [{nombre}] {line}")

        with _lock:
            fue_cancelado = eid in _cancelados
            _cancelados.discard(eid)

        if fue_cancelado:
            ejecucion.estado = "cancelado"
            ejecucion.error = "Cancelado manualmente."
            logger.warning(f"[{nombre}] Cancelado (grupo {grupo_id[:8]}).")
        elif proc.returncode == 0:
            ejecucion.estado = "exitoso"
            logger.info(f"[{nombre}] Completado exitosamente (grupo {grupo_id[:8]}).")
        else:
            ejecucion.estado = "fallido"
            ejecucion.error = f"Código de salida {proc.returncode}. Ver salida para detalles."
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
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
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
