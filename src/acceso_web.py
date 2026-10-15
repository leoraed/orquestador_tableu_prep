"""
Login, permisos y auditoría de la app web.

- control_acceso: dependencia global. Busca el permiso de la ruta en
  permisos.REGLAS (lo que no figura queda denegado), carga el usuario de la
  sesión, verifica CSRF (origen) en los pedidos que modifican algo y deja el
  usuario en request.state.usuario para las plantillas.
- AuditoriaMiddleware: registra quién hizo cada pedido que modifica algo.
- router: /login, /logout, /perfil, /usuarios, /auditoria y sus APIs.
"""
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from src import usuarios as svc
from src.catalog.db import SessionCatalog, get_catalog_db
from src.catalog.models_acceso import Auditoria, Usuario
from src.permisos import PERMISOS, PUBLICO, REGLAS, ROLES, SESION

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SESION_HORAS = 8
_METODOS_SEGUROS = {"GET", "HEAD", "OPTIONS"}


def clave_sesion(base_dir: Path) -> str:
    """APP_SECRET_KEY, o una clave generada y guardada en data/.secret_key (sobrevive reinicios)."""
    clave = os.getenv("APP_SECRET_KEY", "").strip()
    if clave:
        return clave
    archivo = base_dir / "data" / ".secret_key"
    if archivo.exists():
        return archivo.read_text(encoding="utf-8").strip()
    archivo.parent.mkdir(parents=True, exist_ok=True)
    clave = secrets.token_urlsafe(48)
    archivo.write_text(clave, encoding="utf-8")
    return clave


class AccesoDenegado(Exception):
    def __init__(self, status: int, mensaje: str, redirigir: str | None = None):
        self.status, self.mensaje, self.redirigir = status, mensaje, redirigir


def _quiere_html(request: Request) -> bool:
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


async def manejar_acceso_denegado(request: Request, exc: AccesoDenegado):
    if _quiere_html(request):
        if exc.redirigir:
            return RedirectResponse(exc.redirigir, status_code=303)
        if exc.status == 401:
            destino = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(destino)}", status_code=303)
        return templates.TemplateResponse(request, "403.html", {"mensaje": exc.mensaje}, status_code=exc.status)
    return JSONResponse({"ok": False, "error": exc.mensaje}, status_code=exc.status)


def _mismo_origen(request: Request) -> bool:
    origen = request.headers.get("origin") or request.headers.get("referer")
    if not origen:
        return False
    return urlparse(origen).netloc == request.headers.get("host", request.url.netloc)


def usuario_actual(request: Request) -> svc.SesionUsuario | None:
    return getattr(request.state, "usuario", None)


async def control_acceso(request: Request, db: Session = Depends(get_catalog_db)):
    ruta = request.scope.get("route")
    plantilla = getattr(ruta, "path", request.url.path)
    regla = REGLAS.get((request.method, plantilla))
    if regla is None and request.method == "HEAD":
        regla = REGLAS.get(("GET", plantilla))
    if regla is None:
        logger.warning("Ruta sin regla de permisos (denegada): %s %s", request.method, plantilla)
        raise AccesoDenegado(403, "Esta acción no está habilitada.")

    if request.method not in _METODOS_SEGUROS and not _mismo_origen(request):
        raise AccesoDenegado(403, "Pedido rechazado: origen no válido.")
    if regla == PUBLICO:
        return

    sesion = request.session
    u = svc.cargar(db, sesion.get("uid"), sesion.get("ver"))
    if u is None:
        request.session.clear()
        raise AccesoDenegado(401, "Tenés que iniciar sesión.")
    usuario = svc.sesion_de(u)
    request.state.usuario = usuario

    libres = {"/perfil", "/api/perfil/password", "/logout"}
    if u.debe_cambiar_password and plantilla not in libres:
        raise AccesoDenegado(403, "Tenés que cambiar tu contraseña antes de seguir.", redirigir="/perfil?cambiar=1")
    if regla != SESION and not usuario.puede(regla):
        raise AccesoDenegado(403, f"Tu usuario no tiene permiso para esto ({PERMISOS.get(regla, regla)}).")


class AuditoriaMiddleware(BaseHTTPMiddleware):
    """Registra los pedidos que modifican algo (POST/PUT/PATCH/DELETE), con su resultado."""

    async def dispatch(self, request: Request, call_next):
        if request.method in _METODOS_SEGUROS or request.url.path in ("/login",):
            return await call_next(request)
        t0 = time.time()
        respuesta = await call_next(request)
        u = request.scope.get("state", {}).get("usuario")
        if u is not None:
            ruta = request.scope.get("route")
            params = dict(request.scope.get("path_params") or {})
            params.update({k: v for k, v in request.query_params.items()})
            db = SessionCatalog()
            try:
                svc.auditar(db, "app", u.nombre, f"{request.method} {getattr(ruta, 'path', request.url.path)}",
                            params, ok=respuesta.status_code < 400, detalle=f"HTTP {respuesta.status_code}",
                            duracion_ms=int((time.time() - t0) * 1000))
            except Exception:
                logger.exception("No se pudo registrar la auditoría de %s", request.url.path)
            finally:
                db.close()
        return respuesta


# ── rutas ────────────────────────────────────────────────────────────────────

router = APIRouter()

# Intentos fallidos por usuario: tras 5 en 15 minutos, se bloquea ese usuario 15 minutos
_INTENTOS: dict[str, list[float]] = {}
_MAX_INTENTOS, _VENTANA = 5, 15 * 60


def _bloqueado(usuario: str) -> bool:
    ahora = time.time()
    _INTENTOS[usuario] = [t for t in _INTENTOS.get(usuario, []) if ahora - t < _VENTANA]
    return len(_INTENTOS[usuario]) >= _MAX_INTENTOS


def _destino_seguro(nxt: str | None) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/"


@router.get("/login")
def pagina_login(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": _destino_seguro(next), "error": None})


@router.post("/login")
def login(request: Request, usuario: str = Form(""), password: str = Form(""), next: str = Form("/"),
          db: Session = Depends(get_catalog_db)):
    clave = usuario.strip().lower()
    error = None
    if _bloqueado(clave):
        error = "Demasiados intentos fallidos. Probá de nuevo en 15 minutos."
    else:
        u = svc.autenticar(db, clave, password)
        if u is None:
            _INTENTOS.setdefault(clave, []).append(time.time())
            error = "Usuario o contraseña incorrectos."
            svc.auditar(db, "app", clave or "(vacío)", "login", ok=False, detalle="credenciales inválidas")
        else:
            _INTENTOS.pop(clave, None)
            db.commit()
            request.session.clear()
            request.session.update({"uid": u.id, "ver": u.sesion_version})
            svc.auditar(db, "app", u.nombre, "login")
            destino = "/perfil?cambiar=1" if u.debe_cambiar_password else _destino_seguro(next)
            return RedirectResponse(destino, status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": _destino_seguro(next), "error": error},
                                      status_code=401)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/perfil")
def pagina_perfil(request: Request, cambiar: int = 0, db: Session = Depends(get_catalog_db)):
    u = db.get(Usuario, usuario_actual(request).id)
    return templates.TemplateResponse(request, "perfil.html", {
        "u": u, "roles": ROLES, "cambiar": bool(cambiar) or u.debe_cambiar_password, "active": "perfil"})


@router.post("/api/perfil/password")
async def api_cambiar_password(request: Request, db: Session = Depends(get_catalog_db)):
    data = await request.json()
    u = db.get(Usuario, usuario_actual(request).id)
    try:
        svc.cambiar_password(db, u, data.get("actual") or "", data.get("nueva") or "")
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    db.commit()
    request.session["ver"] = u.sesion_version     # esta sesión sigue; las demás se cierran
    return JSONResponse({"ok": True})


@router.post("/api/perfil/token-mcp")
def api_token_mcp(request: Request, db: Session = Depends(get_catalog_db)):
    u = db.get(Usuario, usuario_actual(request).id)
    token = svc.generar_token_mcp(u)
    db.commit()
    return JSONResponse({"ok": True, "token": token})


def _usuario_a_dict(u: Usuario) -> dict:
    fmt = lambda d: d.isoformat() if d else None
    return {"id": u.id, "usuario": u.usuario, "nombre": u.nombre, "roles": svc.normalizar_roles(u.roles),
            "activo": u.activo, "debe_cambiar_password": u.debe_cambiar_password,
            "sin_password": u.password_hash is None, "tiene_token_mcp": u.mcp_token_hash is not None,
            "ultimo_login": fmt(u.ultimo_login), "ultimo_uso_mcp": fmt(u.ultimo_uso_mcp)}


@router.get("/usuarios")
def pagina_usuarios(request: Request, db: Session = Depends(get_catalog_db)):
    return templates.TemplateResponse(request, "usuarios.html", {
        "datos": {"usuarios": [_usuario_a_dict(u) for u in db.query(Usuario).order_by(Usuario.nombre).all()],
                  "roles": {k: v["descripcion"] for k, v in ROLES.items()},
                  "yo": usuario_actual(request).id},
        "active": "usuarios"})


@router.get("/api/usuarios")
def api_listar_usuarios(db: Session = Depends(get_catalog_db)):
    return JSONResponse({"ok": True, "usuarios": [_usuario_a_dict(u) for u in
                                                   db.query(Usuario).order_by(Usuario.nombre).all()]})


@router.post("/api/usuarios")
async def api_crear_usuario(request: Request, db: Session = Depends(get_catalog_db)):
    data = await request.json()
    try:
        u, temporal = svc.crear(db, data.get("usuario"), data.get("nombre"), data.get("roles") or [])
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    db.commit()
    return JSONResponse({"ok": True, "usuario": _usuario_a_dict(u), "password_temporal": temporal})


def _usuario_o_error(db: Session, uid: int) -> Usuario:
    u = db.get(Usuario, uid)
    if not u:
        raise AccesoDenegado(404, "Usuario no encontrado.")
    return u


@router.put("/api/usuarios/{uid}")
async def api_editar_usuario(uid: int, request: Request, db: Session = Depends(get_catalog_db)):
    data = await request.json()
    u = _usuario_o_error(db, uid)
    try:
        svc.actualizar(db, u, data.get("nombre"), data.get("roles") or [], bool(data.get("activo", True)))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    db.commit()
    return JSONResponse({"ok": True, "usuario": _usuario_a_dict(u)})


@router.post("/api/usuarios/{uid}/reset-password")
def api_reset_password(uid: int, db: Session = Depends(get_catalog_db)):
    u = _usuario_o_error(db, uid)
    temporal = svc.resetear_password(db, u)
    db.commit()
    return JSONResponse({"ok": True, "password_temporal": temporal})


@router.post("/api/usuarios/{uid}/revocar-token")
def api_revocar_token(uid: int, db: Session = Depends(get_catalog_db)):
    u = _usuario_o_error(db, uid)
    svc.revocar_token_mcp(u)
    db.commit()
    return JSONResponse({"ok": True})


@router.get("/auditoria")
def pagina_auditoria(request: Request, db: Session = Depends(get_catalog_db)):
    nombres = [n for (n,) in db.query(Auditoria.usuario).distinct().order_by(Auditoria.usuario).all()]
    return templates.TemplateResponse(request, "auditoria.html", {"usuarios": nombres, "active": "auditoria"})


@router.get("/api/auditoria")
def api_auditoria(usuario: str = "", origen: str = "", solo_errores: bool = False, limite: int = 200,
                  db: Session = Depends(get_catalog_db)):
    q = db.query(Auditoria).order_by(Auditoria.created_at.desc(), Auditoria.id.desc())
    if usuario:
        q = q.filter(Auditoria.usuario == usuario)
    if origen:
        q = q.filter(Auditoria.origen == origen)
    if solo_errores:
        q = q.filter(Auditoria.ok.is_(False))
    return JSONResponse({"ok": True, "eventos": [{
        "fecha": e.created_at.isoformat() if e.created_at else None, "origen": e.origen, "usuario": e.usuario,
        "accion": e.accion, "parametros": e.parametros, "ok": e.ok, "detalle": e.detalle,
        "duracion_ms": e.duracion_ms} for e in q.limit(min(max(limite, 1), 1000)).all()]})
