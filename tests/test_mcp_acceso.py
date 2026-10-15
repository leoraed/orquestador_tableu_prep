"""MCP: token del usuario de la app, permisos por herramienta, auditoría y autoría."""
import json
import socket
import threading
import time

import anyio
import httpx
import pytest
import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import mcp_server
from src import usuarios
from src.catalog.db import BaseCatalog
from src.catalog.models_acceso import Auditoria, McpUsuario, Usuario
from src.catalog.models_tableros import Tablero
from src.permisos import permisos_de

HTML = "<!DOCTYPE html><html><body>tablero</body></html>"


@pytest.fixture
def Session(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    BaseCatalog.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    monkeypatch.setattr(mcp_server, "SessionCatalog", S)
    return S


def crear(S, usuario, roles=("analista",), con_token=True):
    db = S()
    u, _ = usuarios.crear(db, usuario, usuario.title(), list(roles), password="clave-segura-123")
    token = usuarios.generar_token_mcp(u) if con_token else None
    db.commit()
    db.close()
    return token


def sesion(nombre, *roles):
    return usuarios.SesionUsuario(1, nombre.lower(), nombre, roles, permisos_de(roles))


def como(u):
    return mcp_server._usuario_actual.set(u)


# ── tokens ───────────────────────────────────────────────────────────────────

def test_token_del_usuario(Session):
    token = crear(Session, "ana")
    db = Session()
    u = usuarios.verificar_token_mcp(db, token)
    assert u.nombre == "Ana" and u.puede("tableros.publicar")
    assert usuarios.verificar_token_mcp(db, "orq_otro") is None
    assert usuarios.verificar_token_mcp(db, None) is None
    fila = db.query(Usuario).one()
    assert fila.mcp_token_hash == usuarios.hash_token(token)          # nunca en claro

    nuevo = usuarios.generar_token_mcp(fila); db.commit()
    assert usuarios.verificar_token_mcp(db, token) is None              # el anterior deja de andar
    assert usuarios.verificar_token_mcp(db, nuevo).nombre == "Ana"
    fila.activo = False; db.commit()
    assert usuarios.verificar_token_mcp(db, nuevo) is None              # usuario desactivado
    db.close()


def test_migra_usuarios_mcp_conservando_el_token(Session):
    db = Session()
    db.add(McpUsuario(nombre="Leonardo Raed", rol="admin", token_hash=usuarios.hash_token("orq_viejo"), activo=True))
    db.commit()
    assert usuarios.migrar_usuarios_mcp(db) == ["leonardo.raed"]
    db.commit()
    assert usuarios.migrar_usuarios_mcp(db) == []                       # idempotente
    u = usuarios.buscar(db, "leonardo.raed")
    assert u.roles == "admin" and u.password_hash is None and u.debe_cambiar_password
    assert usuarios.verificar_token_mcp(db, "orq_viejo").nombre == "Leonardo Raed"
    db.close()


# ── middleware ───────────────────────────────────────────────────────────────

async def _eco(scope, receive, send):
    u = mcp_server._usuario()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": (u.nombre if u else "-").encode()})


def test_middleware_exige_token(Session, monkeypatch):
    token = crear(Session, "ana")
    app = mcp_server.AutenticacionMiddleware(_eco)

    async def pedir(headers):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as c:
            return await c.get("/sse", headers=headers)

    assert anyio.run(pedir, {}).status_code == 401
    assert anyio.run(pedir, {"Authorization": "Bearer orq_falso"}).status_code == 401
    assert anyio.run(pedir, {"Authorization": token}).status_code == 401
    r = anyio.run(pedir, {"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.text == "Ana"
    monkeypatch.setattr(mcp_server, "_AUTH_DESACTIVADA", True)
    assert anyio.run(pedir, {}).text.startswith("anónimo")


# ── permisos, auditoría y autoría ────────────────────────────────────────────

def test_permisos_por_herramienta(Session):
    marca = como(sesion("Lucia", "lector"))
    try:
        assert "no tiene permiso para usar consultar" in mcp_server.consultar("m", ["x"], [])
        assert "no tiene permiso para usar publicar_dashboard" in mcp_server.publicar_dashboard("X", HTML)
        assert "No hay tableros" in mcp_server.listar_dashboards()      # ver sí puede
    finally:
        mcp_server._usuario_actual.reset(marca)
    mcp_server._usuario_actual.set(None)
    assert "no tiene permiso" in mcp_server.listar_dashboards()         # sin usuario: nada

    db = Session()
    regs = [(a.usuario, a.accion, a.ok) for a in db.query(Auditoria).order_by(Auditoria.id).all()]
    assert regs[:3] == [("Lucia", "consultar", False), ("Lucia", "publicar_dashboard", False),
                        ("Lucia", "listar_dashboards", True)]
    db.close()


def test_auditoria_resume_html(Session):
    marca = como(sesion("Ana", "analista"))
    try:
        mcp_server.publicar_dashboard("Ventas", HTML)
    finally:
        mcp_server._usuario_actual.reset(marca)
    db = Session()
    a = db.query(Auditoria).one()
    assert (a.origen, a.usuario, a.accion, a.ok) == ("mcp", "Ana", "publicar_dashboard", True)
    assert json.loads(a.parametros)["html"] == f"<{len(HTML)} bytes>"
    db.close()


def test_solo_el_autor_o_quien_administra_tableros(Session):
    def publicar(u, **kw):
        marca = como(u)
        try:
            return mcp_server.publicar_dashboard(kw.pop("titulo", ""), HTML, **kw)
        finally:
            mcp_server._usuario_actual.reset(marca)

    ana, beto, admin = sesion("Ana", "analista"), sesion("Beto", "analista"), sesion("Root", "admin")
    publicar(ana, titulo="Ventas")
    assert "versión 2" in publicar(ana, actualizar="ventas")
    assert "solo su autor o un admin" in publicar(beto, actualizar="ventas")
    assert "versión 3" in publicar(admin, actualizar="ventas")
    db = Session()
    t = db.query(Tablero).filter(Tablero.slug == "ventas").one()
    assert [v.autor for v in t.versiones] == ["Ana", "Ana", "Root"]
    db.close()


# ── punta a punta: servidor SSE real + cliente MCP ───────────────────────────

def _puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_punta_a_punta_con_cliente_mcp(Session):
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    token = crear(Session, "ana")
    puerto = _puerto_libre()
    server = uvicorn.Server(uvicorn.Config(mcp_server.AutenticacionMiddleware(mcp_server.mcp.sse_app()),
                                           host="127.0.0.1", port=puerto, log_level="warning"))
    hilo = threading.Thread(target=server.run, daemon=True)
    hilo.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    url = f"http://127.0.0.1:{puerto}/sse"

    async def llamar(headers):
        async with sse_client(url, headers=headers) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool("publicar_dashboard", {"titulo": "E2E", "html": HTML})
                return res.content[0].text

    try:
        assert "Publiqué el tablero «E2E»" in anyio.run(llamar, {"Authorization": f"Bearer {token}"})
        with pytest.raises(Exception):
            anyio.run(llamar, {})
    finally:
        server.should_exit = True
        hilo.join(timeout=5)

    db = Session()
    assert db.query(Tablero).filter(Tablero.slug == "e2e").one().autor == "Ana"
    assert db.query(Auditoria).filter(Auditoria.accion == "publicar_dashboard").one().usuario == "Ana"
    db.close()
