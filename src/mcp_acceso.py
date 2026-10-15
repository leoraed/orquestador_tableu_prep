"""
Usuarios y tokens del servidor MCP, y auditoría de las herramientas.

Se administra desde el servidor (no desde la app, que todavía no tiene login):

    python -m src.mcp_acceso crear "Leonardo Raed" --rol admin
    python -m src.mcp_acceso listar
    python -m src.mcp_acceso revocar "Leonardo Raed"
    python -m src.mcp_acceso regenerar "Leonardo Raed"
    python -m src.mcp_acceso auditoria [--usuario X] [--limite 50]
"""
import argparse
import hashlib
import json
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from src.catalog.models_acceso import ROLES, McpAuditoria, McpUsuario

PREFIJO = "orq_"


@dataclass(frozen=True)
class Usuario:
    nombre: str
    rol: str

    @property
    def es_admin(self) -> bool:
        return self.rol == "admin"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def nuevo_token() -> str:
    return PREFIJO + secrets.token_urlsafe(32)


def crear_usuario(db: Session, nombre: str, rol: str = "usuario") -> tuple[McpUsuario, str]:
    """Devuelve (usuario, token en claro). El token no se puede recuperar después."""
    nombre = nombre.strip()
    if not nombre:
        raise ValueError("El nombre es obligatorio.")
    if rol not in ROLES:
        raise ValueError(f"Rol inválido: {rol}. Opciones: {', '.join(ROLES)}.")
    if db.query(McpUsuario).filter(McpUsuario.nombre == nombre).first():
        raise ValueError(f"Ya existe el usuario «{nombre}». Usá «regenerar» para darle un token nuevo.")
    token = nuevo_token()
    u = McpUsuario(nombre=nombre, rol=rol, token_hash=hash_token(token), activo=True)
    db.add(u)
    db.flush()
    return u, token


def regenerar_token(db: Session, nombre: str) -> str:
    u = _usuario_o_error(db, nombre)
    token = nuevo_token()
    u.token_hash = hash_token(token)
    u.activo = True
    return token


def revocar(db: Session, nombre: str) -> None:
    _usuario_o_error(db, nombre).activo = False


def _usuario_o_error(db: Session, nombre: str) -> McpUsuario:
    u = db.query(McpUsuario).filter(McpUsuario.nombre == nombre.strip()).first()
    if not u:
        raise ValueError(f"No existe el usuario «{nombre}».")
    return u


def verificar_token(db: Session, token: str | None) -> Usuario | None:
    """Usuario activo dueño del token, o None. Registra el último uso."""
    if not token or not token.startswith(PREFIJO):
        return None
    u = db.query(McpUsuario).filter(McpUsuario.token_hash == hash_token(token),
                                    McpUsuario.activo.is_(True)).first()
    if not u:
        return None
    u.ultimo_uso = datetime.utcnow()
    db.commit()
    return Usuario(u.nombre, u.rol)


# ── Auditoría ────────────────────────────────────────────────────────────────

# Parámetros que no se guardan completos (contenido grande o sin valor de auditoría)
_RESUMIR = {"html"}


def resumir_parametros(params: dict) -> str:
    limpio = {}
    for k, v in params.items():
        if k in _RESUMIR and isinstance(v, str):
            limpio[k] = f"<{len(v.encode('utf-8'))} bytes>"
        else:
            limpio[k] = v
    texto = json.dumps(limpio, ensure_ascii=False, default=str)
    return texto if len(texto) <= 4000 else texto[:4000] + "…"


def registrar(db: Session, usuario: str, herramienta: str, params: dict, ok: bool,
              detalle: str = "", duracion_ms: int | None = None) -> None:
    db.add(McpAuditoria(usuario=usuario, herramienta=herramienta, parametros=resumir_parametros(params),
                        ok=ok, detalle=(detalle or "")[:500] or None, duracion_ms=duracion_ms))
    db.commit()


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv
    load_dotenv()
    from src.catalog.db import BaseCatalog, SessionCatalog, engine_catalog
    from src.catalog import models_acceso  # noqa: F401  registra las tablas

    ap = argparse.ArgumentParser(prog="python -m src.mcp_acceso", description="Usuarios del servidor MCP.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crear", help="crea un usuario y muestra su token")
    c.add_argument("nombre"); c.add_argument("--rol", default="usuario", choices=ROLES)
    sub.add_parser("listar", help="lista los usuarios")
    r = sub.add_parser("revocar", help="desactiva el token de un usuario"); r.add_argument("nombre")
    g = sub.add_parser("regenerar", help="da un token nuevo (el anterior deja de funcionar)"); g.add_argument("nombre")
    a = sub.add_parser("auditoria", help="últimas llamadas a herramientas")
    a.add_argument("--usuario"); a.add_argument("--limite", type=int, default=50)
    args = ap.parse_args(argv)

    BaseCatalog.metadata.create_all(bind=engine_catalog, tables=[McpUsuario.__table__, McpAuditoria.__table__])
    db = SessionCatalog()
    try:
        if args.cmd in ("crear", "regenerar"):
            if args.cmd == "crear":
                u, token = crear_usuario(db, args.nombre, args.rol)
                print(f"Usuario «{u.nombre}» creado (rol {u.rol}).")
            else:
                token = regenerar_token(db, args.nombre)
                print(f"Token nuevo para «{args.nombre}». El anterior ya no funciona.")
            db.commit()
            print(f"\nToken (se muestra una sola vez, guardalo ahora):\n\n  {token}\n")
            print("En el cliente MCP (mcp-remote):")
            print('  "args": ["mcp-remote@latest", "http://<servidor>:8001/sse", "--allow-http",')
            print('           "--header", "Authorization:${ORQ_MCP_AUTH}"],')
            print(f'  "env": {{"ORQ_MCP_AUTH": "Bearer {token}"}}')
        elif args.cmd == "revocar":
            revocar(db, args.nombre); db.commit()
            print(f"Token de «{args.nombre}» revocado.")
        elif args.cmd == "listar":
            for u in db.query(McpUsuario).order_by(McpUsuario.nombre).all():
                uso = u.ultimo_uso.strftime("%d/%m/%Y %H:%M") if u.ultimo_uso else "nunca"
                print(f"{u.nombre:<30} {u.rol:<8} {'activo' if u.activo else 'REVOCADO':<9} último uso: {uso}")
        elif args.cmd == "auditoria":
            q = db.query(McpAuditoria).order_by(McpAuditoria.created_at.desc())
            if args.usuario:
                q = q.filter(McpAuditoria.usuario == args.usuario)
            for e in reversed(q.limit(args.limite).all()):
                estado = "ok " if e.ok else "ERR"
                print(f"{e.created_at:%d/%m %H:%M:%S} {estado} {e.usuario:<20} {e.herramienta:<20} {e.parametros}"
                      + (f"  → {e.detalle}" if e.detalle and not e.ok else ""))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
