"""
Usuarios de la app: contraseñas, roles, token del MCP y auditoría.

Administración desde el servidor (el resto se hace en la pantalla Usuarios):

    python -m src.usuarios crear-admin <usuario> --nombre "Nombre Apellido"
    python -m src.usuarios reset-password <usuario>
    python -m src.usuarios listar
    python -m src.usuarios auditoria [--usuario "Nombre"] [--limite 50]
"""
import argparse
import base64
import hashlib
import hmac
import json
import re
import secrets
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from src.catalog.models_acceso import Auditoria, McpUsuario, Usuario
from src.permisos import ROLES, normalizar_roles, permisos_de

PREFIJO_TOKEN = "orq_"
MIN_PASSWORD = 10


@dataclass(frozen=True)
class SesionUsuario:
    id: int
    usuario: str
    nombre: str
    roles: tuple[str, ...]
    permisos: frozenset[str]

    def puede(self, permiso: str) -> bool:
        return permiso in self.permisos


def sesion_de(u: Usuario) -> SesionUsuario:
    roles = tuple(normalizar_roles(u.roles or ""))
    return SesionUsuario(u.id, u.usuario, u.nombre, roles, permisos_de(roles))


# ── contraseñas ──────────────────────────────────────────────────────────────

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 64}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${dk}".format(
        **_SCRYPT, salt=base64.b64encode(salt).decode(), dk=base64.b64encode(dk).decode())


def verificar_password(password: str, guardado: str | None) -> bool:
    if not guardado or not password:
        return False
    try:
        alg, n, r, p, salt, dk = guardado.split("$")
        if alg != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r),
                              p=int(p), dklen=len(base64.b64decode(dk)))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def validar_password(password: str, usuario: str = "") -> None:
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"La contraseña tiene que tener al menos {MIN_PASSWORD} caracteres.")
    if usuario and usuario.lower() in password.lower():
        raise ValueError("La contraseña no puede contener el nombre de usuario.")


def password_temporal() -> str:
    return secrets.token_urlsafe(9)   # 12 caracteres


def slug_usuario(nombre: str) -> str:
    s = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", ".", s).strip(".")[:100] or "usuario"


# ── alta, edición y sesión ───────────────────────────────────────────────────

def buscar(db: Session, usuario: str) -> Usuario | None:
    return db.query(Usuario).filter(Usuario.usuario == (usuario or "").strip().lower()).first()


def crear(db: Session, usuario: str, nombre: str, roles, password: str | None = None) -> tuple[Usuario, str | None]:
    """Crea el usuario. Sin password genera una temporal (que se devuelve) y obliga a cambiarla."""
    usuario = (usuario or "").strip().lower()
    nombre = (nombre or "").strip()
    if not re.fullmatch(r"[a-z0-9._-]{3,100}", usuario):
        raise ValueError("El usuario tiene que tener entre 3 y 100 caracteres: letras, números, punto, guion.")
    if not nombre:
        raise ValueError("El nombre es obligatorio.")
    roles = normalizar_roles(roles)
    if not roles:
        raise ValueError("Asigná al menos un rol.")
    if buscar(db, usuario):
        raise ValueError(f"Ya existe el usuario «{usuario}».")
    if db.query(Usuario).filter(Usuario.nombre == nombre).first():
        raise ValueError(f"Ya existe un usuario con el nombre «{nombre}».")
    temporal = None
    if password is None:
        temporal = password = password_temporal()
    else:
        validar_password(password, usuario)
    u = Usuario(usuario=usuario, nombre=nombre, roles=",".join(roles), password_hash=hash_password(password),
                activo=True, debe_cambiar_password=temporal is not None, sesion_version=1)
    db.add(u)
    db.flush()
    return u, temporal


def autenticar(db: Session, usuario: str, password: str) -> Usuario | None:
    u = buscar(db, usuario)
    if not u or not u.activo or not verificar_password(password, u.password_hash):
        return None
    u.ultimo_login = datetime.utcnow()
    return u


def cargar(db: Session, uid, version) -> Usuario | None:
    """Usuario de una sesión, si sigue activo y la sesión no fue invalidada."""
    if not isinstance(uid, int):
        return None
    u = db.get(Usuario, uid)
    if not u or not u.activo or u.sesion_version != version:
        return None
    return u


def cambiar_password(db: Session, u: Usuario, actual: str, nueva: str) -> None:
    if not verificar_password(actual, u.password_hash):
        raise ValueError("La contraseña actual no es correcta.")
    if actual == nueva:
        raise ValueError("La contraseña nueva tiene que ser distinta de la actual.")
    validar_password(nueva, u.usuario)
    u.password_hash = hash_password(nueva)
    u.debe_cambiar_password = False
    u.sesion_version += 1     # cierra las otras sesiones abiertas


def resetear_password(db: Session, u: Usuario) -> str:
    temporal = password_temporal()
    u.password_hash = hash_password(temporal)
    u.debe_cambiar_password = True
    u.sesion_version += 1
    return temporal


def _admins_activos(db: Session) -> list[Usuario]:
    return [u for u in db.query(Usuario).filter(Usuario.activo.is_(True)).all()
            if "admin" in normalizar_roles(u.roles)]


def actualizar(db: Session, u: Usuario, nombre: str, roles, activo: bool) -> None:
    nombre = (nombre or "").strip()
    if not nombre:
        raise ValueError("El nombre es obligatorio.")
    otro = db.query(Usuario).filter(Usuario.nombre == nombre, Usuario.id != u.id).first()
    if otro:
        raise ValueError(f"Ya existe un usuario con el nombre «{nombre}».")
    roles = normalizar_roles(roles)
    if not roles:
        raise ValueError("Asigná al menos un rol.")
    era_admin_activo = u.activo and "admin" in normalizar_roles(u.roles)
    sigue_admin_activo = activo and "admin" in roles
    if era_admin_activo and not sigue_admin_activo and len(_admins_activos(db)) <= 1:
        raise ValueError("Tiene que quedar al menos un admin activo.")
    if u.activo and not activo:
        u.sesion_version += 1
    u.nombre, u.roles, u.activo = nombre, ",".join(roles), activo


# ── token del MCP ────────────────────────────────────────────────────────────

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generar_token_mcp(u: Usuario) -> str:
    """Token nuevo (el anterior deja de funcionar). Se muestra una sola vez."""
    token = PREFIJO_TOKEN + secrets.token_urlsafe(32)
    u.mcp_token_hash = hash_token(token)
    u.mcp_token_creado = datetime.utcnow()
    return token


def revocar_token_mcp(u: Usuario) -> None:
    u.mcp_token_hash = None
    u.mcp_token_creado = None


def verificar_token_mcp(db: Session, token: str | None) -> SesionUsuario | None:
    if not token or not token.startswith(PREFIJO_TOKEN):
        return None
    u = db.query(Usuario).filter(Usuario.mcp_token_hash == hash_token(token), Usuario.activo.is_(True)).first()
    if not u:
        return None
    ahora = datetime.utcnow()
    if not u.ultimo_uso_mcp or ahora - u.ultimo_uso_mcp > timedelta(minutes=1):
        u.ultimo_uso_mcp = ahora
        db.commit()
    return sesion_de(u)


def migrar_usuarios_mcp(db: Session) -> list[str]:
    """
    Pasa los usuarios de la primera versión del MCP (mcp_usuarios) a `usuarios`,
    conservando su token. Quedan sin contraseña: hay que darles una con reset-password.
    """
    migrados = []
    for m in db.query(McpUsuario).all():
        if db.query(Usuario).filter((Usuario.mcp_token_hash == m.token_hash) | (Usuario.nombre == m.nombre)).first():
            continue
        base, usuario, n = slug_usuario(m.nombre), slug_usuario(m.nombre), 2
        while buscar(db, usuario):
            usuario, n = f"{base}{n}", n + 1
        db.add(Usuario(usuario=usuario, nombre=m.nombre, roles="admin" if m.rol == "admin" else "analista",
                       password_hash=None, activo=m.activo, debe_cambiar_password=True, sesion_version=1,
                       mcp_token_hash=m.token_hash, mcp_token_creado=m.created_at, created_at=m.created_at))
        migrados.append(usuario)
    db.flush()
    return migrados


# ── auditoría ────────────────────────────────────────────────────────────────

_NO_GUARDAR = {"password", "actual", "nueva", "token"}
_SOLO_TAMANIO = {"html"}


def resumir_parametros(params: dict | None) -> str | None:
    if not params:
        return None
    limpio = {}
    for k, v in params.items():
        if k in _NO_GUARDAR:
            limpio[k] = "***"
        elif k in _SOLO_TAMANIO and isinstance(v, str):
            limpio[k] = f"<{len(v.encode('utf-8'))} bytes>"
        else:
            limpio[k] = v
    texto = json.dumps(limpio, ensure_ascii=False, default=str)
    return texto if len(texto) <= 4000 else texto[:4000] + "…"


def auditar(db: Session, origen: str, usuario: str, accion: str, params: dict | None = None,
            ok: bool = True, detalle: str = "", duracion_ms: int | None = None) -> None:
    db.add(Auditoria(origen=origen, usuario=usuario, accion=accion[:255], parametros=resumir_parametros(params),
                     ok=ok, detalle=(detalle or "")[:500] or None, duracion_ms=duracion_ms))
    db.commit()


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv
    load_dotenv()
    from src.catalog.db import BaseCatalog, SessionCatalog, engine_catalog
    from src.catalog.models_acceso import McpAuditoria

    ap = argparse.ArgumentParser(prog="python -m src.usuarios", description="Usuarios del orquestador.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crear-admin", help="crea un usuario admin con contraseña temporal")
    c.add_argument("usuario"); c.add_argument("--nombre", required=True)
    r = sub.add_parser("reset-password", help="contraseña temporal nueva (obliga a cambiarla)")
    r.add_argument("usuario")
    sub.add_parser("listar", help="lista los usuarios")
    a = sub.add_parser("auditoria", help="últimas acciones de la app y del MCP")
    a.add_argument("--usuario"); a.add_argument("--limite", type=int, default=50)
    args = ap.parse_args(argv)

    BaseCatalog.metadata.create_all(bind=engine_catalog, tables=[
        Usuario.__table__, Auditoria.__table__, McpUsuario.__table__, McpAuditoria.__table__])
    db = SessionCatalog()
    try:
        migrar_usuarios_mcp(db)
        db.commit()
        if args.cmd == "crear-admin":
            u, temporal = crear(db, args.usuario, args.nombre, ["admin"])
            db.commit()
            print(f"Admin «{u.usuario}» ({u.nombre}) creado.\nContraseña temporal: {temporal}\n"
                  "Se va a pedir cambiarla en el primer ingreso.")
        elif args.cmd == "reset-password":
            u = buscar(db, args.usuario)
            if not u:
                raise ValueError(f"No existe el usuario «{args.usuario}». Usá «listar».")
            temporal = resetear_password(db, u)
            u.activo = True
            db.commit()
            print(f"Contraseña temporal de «{u.usuario}»: {temporal}\nSe va a pedir cambiarla al ingresar.")
        elif args.cmd == "listar":
            for u in db.query(Usuario).order_by(Usuario.usuario).all():
                login = u.ultimo_login.strftime("%d/%m/%Y %H:%M") if u.ultimo_login else "nunca"
                print(f"{u.usuario:<22} {u.nombre:<26} {u.roles:<28} {'activo' if u.activo else 'INACTIVO':<9} "
                      f"MCP: {'sí' if u.mcp_token_hash else 'no':<3} último login: {login}"
                      + ("  (sin contraseña)" if not u.password_hash else ""))
        elif args.cmd == "auditoria":
            q = db.query(Auditoria).order_by(Auditoria.created_at.desc())
            if args.usuario:
                q = q.filter(Auditoria.usuario == args.usuario)
            for e in reversed(q.limit(args.limite).all()):
                print(f"{e.created_at:%d/%m %H:%M:%S} {e.origen:<3} {'ok ' if e.ok else 'ERR'} {e.usuario:<22} "
                      f"{e.accion:<40} {e.parametros or ''}" + (f"  → {e.detalle}" if e.detalle and not e.ok else ""))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
