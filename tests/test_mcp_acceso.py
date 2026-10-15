"""Tokens del MCP, auditoría y autoría de tableros (catálogo SQLite en memoria)."""
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
from src import mcp_acceso as acceso
from src.catalog.db import BaseCatalog
from src.catalog.models_acceso import McpAuditoria
from src.catalog.models_tableros import Tablero

HTML = "<!DOCTYPE html><html><body>tablero</body></html>"


@pytest.fixture
def Session(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    BaseCatalog.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    monkeypatch.setattr(mcp_server, "SessionCatalog", S)
    return S


def crear(S, nombre, rol="usuario"):
    db = S()
    _, token = acceso.crear_usuario(db, nombre, rol)
    db.commit()
    db.close()
    return token


def como(usuario):
    return mcp_server._usuario_actual.set(usuario)


# ── tokens ───────────────────────────────────────────────────────────────────

def test_tokens_crear_verificar_revocar_regenerar(Session):
    token = crear(Session, "Ana")
    db = Session()
    assert token.startswith("orq_")
    assert acceso.verificar_token(db, token) == acceso.Usuario("Ana", "usuario")
    assert acceso.verificar_token(db, "orq_otro") is None
    assert acceso.verificar_token(db, None) is None
    # El token no se guarda en claro
    from src.catalog.models_acceso import McpUsuario
    assert db.query(McpUsuario).one().token_hash == acceso.hash_token(token)

    with pytest.raises(ValueError, match="Ya existe"):
        acceso.crear_usuario(db, "Ana")
    with pytest.raises(ValueError, match="Rol inválido"):
        acceso.crear_usuario(db, "Beto", "jefe")

    acceso.revocar(db, "Ana"); db.commit()
    assert acceso.verificar_token(db, token) is None
    nuevo = acceso.regenerar_token(db, "Ana"); db.commit()
    assert acceso.verificar_token(db, nuevo).nombre == "Ana"
    assert acceso.verificar_token(db, token) is None
    db.close()


# ── middleware ───────────────────────────────────────────────────────────────

async def _eco(scope, receive, send):
    """App mínima que responde con el usuario que dejó el middleware."""
    u = mcp_server._usuario()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": (u.nombre if u else "-").encode()})


def test_middleware_exige_token(Session, monkeypatch):
    token = crear(Session, "Ana")
    app = mcp_server.AutenticacionMiddleware(_eco)

    async def pedir(headers):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as c:
            return await c.get("/sse", headers=headers)

    assert anyio.run(pedir, {}).status_code == 401
    assert anyio.run(pedir, {"Authorization": "Bearer orq_falso"}).status_code == 401
    assert anyio.run(pedir, {"Authorization": token}).status_code == 401            # sin "Bearer"
    r = anyio.run(pedir, {"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.text == "Ana"

    monkeypatch.setattr(mcp_server, "_AUTH_DESACTIVADA", True)
    assert anyio.run(pedir, {}).text.startswith("anónimo")


# ── auditoría y autoría ──────────────────────────────────────────────────────

def test_auditoria_registra_usuario_y_resume_html(Session):
    marca = como(acceso.Usuario("Ana", "usuario"))
    try:
        mcp_server.publicar_dashboard("Ventas", HTML)
        mcp_server.publicar_dashboard("", HTML, actualizar="no-existe")
    finally:
        mcp_server._usuario_actual.reset(marca)
    db = Session()
    regs = db.query(McpAuditoria).order_by(McpAuditoria.id).all()
    assert [(r.usuario, r.herramienta, r.ok) for r in regs] == [
        ("Ana", "publicar_dashboard", True), ("Ana", "publicar_dashboard", False)]
    params = json.loads(regs[0].parametros)
    assert params["html"] == f"<{len(HTML)} bytes>" and params["titulo"] == "Ventas"
    assert "no existe" in regs[1].detalle
    db.close()


def test_solo_el_autor_o_un_admin_actualizan(Session):
    def publicar(usuario, **kw):
        marca = como(usuario)
        try:
            return mcp_server.publicar_dashboard(kw.pop("titulo", ""), HTML, **kw)
        finally:
            mcp_server._usuario_actual.reset(marca)

    ana, beto, admin = acceso.Usuario("Ana", "usuario"), acceso.Usuario("Beto", "usuario"), acceso.Usuario("Root", "admin")
    publicar(ana, titulo="Ventas")
    assert "versión 2" in publicar(ana, actualizar="ventas")
    assert "solo su autor o un admin" in publicar(beto, actualizar="ventas")
    assert "versión 3" in publicar(admin, actualizar="ventas")

    # Tablero subido desde la app (sin autor): solo admins
    db = Session()
    from src.catalog import tableros_service as svc
    svc.crear(db, "Desde la app", HTML); db.commit(); db.close()
    assert "se subió desde la app" in publicar(ana, actualizar="desde-la-app")
    assert "versión 2" in publicar(admin, actualizar="desde-la-app")

    db = Session()
    t = db.query(Tablero).filter(Tablero.slug == "ventas").one()
    assert t.autor == "Ana" and [v.autor for v in t.versiones] == ["Ana", "Ana", "Root"]
    db.close()
    assert "autor Ana" in mcp_server.listar_dashboards()

    mcp_server._usuario_actual.set(None)
    assert "no hay un usuario autenticado" in mcp_server.publicar_dashboard("X", HTML)


# ── punta a punta: servidor SSE real + cliente MCP ───────────────────────────

def _puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_punta_a_punta_con_cliente_mcp(Session):
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    token = crear(Session, "Ana")
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
        texto = anyio.run(llamar, {"Authorization": f"Bearer {token}"})
        assert "Publiqué el tablero «E2E»" in texto
        with pytest.raises(Exception):
            anyio.run(llamar, {})          # sin token: el servidor responde 401 al conectar
    finally:
        server.should_exit = True
        hilo.join(timeout=5)

    db = Session()
    assert db.query(Tablero).filter(Tablero.slug == "e2e").one().autor == "Ana"   # el usuario llegó a la herramienta
    assert db.query(McpAuditoria).filter(McpAuditoria.herramienta == "publicar_dashboard").one().usuario == "Ana"
    db.close()
