"""Login, permisos por rol, CSRF, contraseñas temporales, usuarios y auditoría de la app."""
import io

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.api as api
from conftest import PASSWORD, iniciar_sesion
from src import acceso_web, usuarios
from src.catalog import models as _catalog_models  # noqa: F401
from src.catalog.db import BaseCatalog, get_catalog_db
from src.catalog.models_acceso import Auditoria, Usuario
from src.permisos import REGLAS

HTML = "<html><body>t</body></html>"


@pytest.fixture
def Session(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    BaseCatalog.metadata.create_all(engine)
    S = sessionmaker(bind=engine)

    def override():
        s = S()
        try:
            yield s
        finally:
            s.close()

    api.app.dependency_overrides[get_catalog_db] = override
    monkeypatch.setattr(acceso_web, "SessionCatalog", S)
    acceso_web._INTENTOS.clear()
    yield S
    api.app.dependency_overrides.clear()


def cliente(Session, monkeypatch, *roles, usuario=None):
    usuario = usuario or "u." + "-".join(roles)
    return iniciar_sesion(TestClient(api.app), Session, monkeypatch, roles=roles, usuario=usuario,
                          nombre=usuario.title())


HTML_ACCEPT = {"accept": "text/html"}


def test_todas_las_rutas_tienen_regla():
    faltan = [(m, r.path) for r in api.app.routes if isinstance(r, APIRoute)
              for m in r.methods if m != "HEAD" and (m, r.path) not in REGLAS]
    assert faltan == [], f"Rutas sin regla en src/permisos.py: {faltan}"
    sobran = [k for k in REGLAS if not any(isinstance(r, APIRoute) and r.path == k[1] and k[0] in r.methods
                                           for r in api.app.routes)]
    assert sobran == [], f"Reglas de rutas que no existen: {sobran}"


def test_sin_sesion(Session):
    c = TestClient(api.app)
    r = c.get("/modelos", headers=HTML_ACCEPT, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/modelos"
    assert c.get("/api/tableros").status_code == 401
    assert c.get("/tableros/x/contenido").status_code == 401        # los datos embebidos también
    assert c.get("/login").status_code == 200
    assert c.get("/health").status_code == 200
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404


def test_login_fallido_y_bloqueo(Session, monkeypatch):
    cliente(Session, monkeypatch, "lector", usuario="ana")
    c = TestClient(api.app, headers={"origin": "http://testserver"})
    for _ in range(5):
        assert c.post("/login", data={"usuario": "ana", "password": "mal"}).status_code == 401
    r = c.post("/login", data={"usuario": "ana", "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 401 and "Demasiados intentos" in r.text            # bloqueado aunque sea correcta
    db = Session()
    assert db.query(Auditoria).filter(Auditoria.accion == "login", Auditoria.ok.is_(False)).count() == 5
    db.close()


def test_csrf(Session, monkeypatch):
    c = cliente(Session, monkeypatch, "admin")
    assert c.post("/api/tableros/x/versiones", headers={"origin": "http://otro.sitio"}).status_code == 403
    del c.headers["origin"]
    assert c.post("/logout", follow_redirects=False).status_code == 403          # sin origen tampoco
    c.headers["referer"] = "http://testserver/perfil"
    assert c.post("/logout", follow_redirects=False).status_code == 303          # referer del mismo sitio


def test_permisos_por_rol(Session, monkeypatch):
    lector = cliente(Session, monkeypatch, "lector")
    assert lector.get("/tableros").status_code == 200
    assert lector.get("/", headers=HTML_ACCEPT, follow_redirects=False).headers["location"] == "/tableros"
    assert lector.get("/flows", headers=HTML_ACCEPT).status_code == 403
    assert lector.post("/api/modelos/m/probar", json={}).status_code == 403
    assert lector.post("/api/tableros", data={"titulo": "x"},
                       files={"file": ("x.html", io.BytesIO(b"<p>"))}).status_code == 403
    assert lector.get("/usuarios", headers=HTML_ACCEPT).status_code == 403
    assert "Usuarios" not in lector.get("/tableros").text                       # el menú no lo muestra

    operador = cliente(Session, monkeypatch, "operador")
    assert operador.get("/flows").status_code == 200
    assert operador.post("/api/flows", data={}).status_code == 403              # configurar ≠ ejecutar
    assert operador.get("/configuracion").status_code == 403

    modelador = cliente(Session, monkeypatch, "modelador")
    assert modelador.post("/api/modelos/m/probar", json={}).status_code != 403
    assert modelador.get("/configuracion").status_code == 403


def test_autoria_de_tableros_en_la_app(Session, monkeypatch):
    def publicar(c):
        return c.post("/api/tableros", data={"titulo": "Ventas"},
                      files={"file": ("v.html", io.BytesIO(HTML.encode()), "text/html")}).json()

    ana = cliente(Session, monkeypatch, "analista", usuario="ana")
    t = publicar(ana)["tablero"]
    assert t["autor"] == "Ana"
    beto = cliente(Session, monkeypatch, "analista", usuario="beto")
    assert beto.delete(f"/api/tableros/{t['slug']}").status_code == 403
    assert beto.put(f"/api/tableros/{t['slug']}", json={"titulo": "Mío"}).status_code == 403
    admin = cliente(Session, monkeypatch, "admin")
    assert admin.put(f"/api/tableros/{t['slug']}", json={"titulo": "Ventas 2026"}).json()["ok"]

    db = Session()
    eventos = [(a.usuario, a.accion, a.ok) for a in db.query(Auditoria).filter(Auditoria.origen == "app",
                                                                               Auditoria.accion != "login")]
    assert ("Beto", "DELETE /api/tableros/{slug}", False) in eventos
    assert ("U.Admin", "PUT /api/tableros/{slug}", True) in eventos
    db.close()


def test_contrasenia_temporal_y_sesiones(Session, monkeypatch):
    admin = cliente(Session, monkeypatch, "admin")
    r = admin.post("/api/usuarios", json={"usuario": "ana", "nombre": "Ana Pérez", "roles": ["analista"]}).json()
    assert r["ok"] and len(r["password_temporal"]) >= 10
    temporal = r["password_temporal"]

    ana = TestClient(api.app, headers={"origin": "http://testserver"})
    login = ana.post("/login", data={"usuario": "ana", "password": temporal}, follow_redirects=False)
    assert login.headers["location"] == "/perfil?cambiar=1"
    assert ana.get("/tableros", headers=HTML_ACCEPT, follow_redirects=False).headers["location"] == "/perfil?cambiar=1"
    assert ana.post("/api/perfil/token-mcp").status_code == 403                 # primero la contraseña
    assert not ana.post("/api/perfil/password", json={"actual": temporal, "nueva": "corta"}).json()["ok"]
    assert not ana.post("/api/perfil/password", json={"actual": temporal, "nueva": "ana-1234567890"}).json()["ok"]
    otra = TestClient(api.app, headers={"origin": "http://testserver"})
    otra.post("/login", data={"usuario": "ana", "password": temporal})
    assert ana.post("/api/perfil/password", json={"actual": temporal, "nueva": "una-clave-larga"}).json()["ok"]
    assert ana.get("/tableros").status_code == 200                               # esta sesión sigue
    assert otra.get("/api/tableros").status_code == 401                          # la otra se cerró

    token = ana.post("/api/perfil/token-mcp").json()["token"]
    db = Session()
    assert usuarios.verificar_token_mcp(db, token).nombre == "Ana Pérez"
    db.close()


def test_admin_de_usuarios(Session, monkeypatch):
    admin = cliente(Session, monkeypatch, "admin", usuario="root")
    db = Session()
    yo = usuarios.buscar(db, "root").id
    db.close()
    r = admin.put(f"/api/usuarios/{yo}", json={"nombre": "Root", "roles": ["analista"], "activo": True}).json()
    assert not r["ok"] and "al menos un admin" in r["error"]
    assert not admin.post("/api/usuarios", json={"usuario": "x", "nombre": "X", "roles": ["jefe"]}).json()["ok"]

    nuevo = admin.post("/api/usuarios", json={"usuario": "lucia", "nombre": "Lucía", "roles": ["lector"]}).json()["usuario"]
    lu = TestClient(api.app, headers={"origin": "http://testserver"})
    db = Session()
    u = db.get(Usuario, nuevo["id"]); u.password_hash = usuarios.hash_password(PASSWORD); u.debe_cambiar_password = False
    db.commit(); db.close()
    lu.post("/login", data={"usuario": "lucia", "password": PASSWORD})
    assert lu.get("/api/tableros").status_code == 200
    admin.put(f"/api/usuarios/{nuevo['id']}", json={"nombre": "Lucía", "roles": ["lector"], "activo": False})
    assert lu.get("/api/tableros").status_code == 401                            # desactivado: afuera
