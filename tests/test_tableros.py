"""Tableros publicados (catálogo SQLite en memoria)."""
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.api as api
from conftest import iniciar_sesion
from src.catalog import models as _catalog_models  # noqa: F401  registra tablas del catálogo
from src.catalog.db import BaseCatalog, get_catalog_db

HTML = "<!DOCTYPE html><html><body><h1>NOC</h1><script>document.title='x'</script></body></html>"


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    BaseCatalog.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    api.app.dependency_overrides[get_catalog_db] = override
    yield iniciar_sesion(TestClient(api.app), Session, monkeypatch)
    api.app.dependency_overrides.clear()


def publicar(client, titulo="NOC · Actividad", html=HTML, nombre="noc.html", **extra):
    return client.post("/api/tableros", data={"titulo": titulo, **extra},
                       files={"file": (nombre, io.BytesIO(html.encode("utf-8")), "text/html")}).json()


def test_publicar_y_ver(client):
    r = publicar(client, descripcion="Actividad del NOC", modelo="stagedata_noc_link_orbith_bi",
                 tags="noc, operadores", datos_hasta="2026-10-13")
    assert r["ok"]
    t = r["tablero"]
    assert t["slug"] == "noc-actividad"
    assert t["tags"] == ["noc", "operadores"] and t["version"] == 1 and t["datos_hasta"] == "2026-10-13"

    assert "NOC · Actividad" in client.get("/tableros").text
    assert client.get("/tableros/noc-actividad").status_code == 200
    assert client.get("/tableros/no-existe").status_code == 404


def test_contenido_se_sirve_aislado(client):
    publicar(client)
    r = client.get("/tableros/noc-actividad/contenido")
    assert r.text == HTML
    csp = r.headers["content-security-policy"]
    assert csp.startswith("sandbox ") and "allow-scripts" in csp
    assert "allow-same-origin" not in csp     # sin esto el tablero no puede usar la API de la app
    assert r.headers["x-content-type-options"] == "nosniff"
    descarga = client.get("/tableros/noc-actividad/contenido?descargar=true")
    assert "attachment" in descarga.headers["content-disposition"]


def test_versiones(client):
    publicar(client)
    r = client.post("/api/tableros/noc-actividad/versiones", data={"nota": "datos a noviembre", "datos_hasta": "2026-11-30"},
                    files={"file": ("v2.html", io.BytesIO(b"<html>v2</html>"), "text/html")}).json()
    assert r["ok"] and r["tablero"]["version"] == 2
    assert [v["version"] for v in r["tablero"]["versiones"]] == [2, 1]
    assert client.get("/tableros/noc-actividad/contenido").text == "<html>v2</html>"
    assert client.get("/tableros/noc-actividad/contenido?version=1").text == HTML
    assert client.get("/tableros/noc-actividad/contenido?version=9").status_code == 404


def test_slug_unico_y_validaciones(client):
    publicar(client)
    assert publicar(client)["tablero"]["slug"] == "noc-actividad-2"
    assert "obligatorio" in publicar(client, titulo="  ")["error"]
    assert ".html" in publicar(client, nombre="datos.csv")["error"]
    assert "AAAA-MM-DD" in publicar(client, datos_hasta="13/10/2026")["error"]
    malo = client.post("/api/tableros", data={"titulo": "x"},
                       files={"file": ("x.html", io.BytesIO("ñ".encode("latin-1")), "text/html")}).json()
    assert "UTF-8" in malo["error"]


def test_editar_y_eliminar(client):
    publicar(client)
    r = client.put("/api/tableros/noc-actividad", json={"titulo": "NOC", "descripcion": "d", "modelo": "",
                                                        "tags": " a , ,b "}).json()
    assert r["ok"] and r["tablero"]["titulo"] == "NOC" and r["tablero"]["tags"] == ["a", "b"]
    assert r["tablero"]["slug"] == "noc-actividad"   # el slug no cambia: los links siguen andando
    assert client.delete("/api/tableros/noc-actividad").json()["ok"]
    assert client.get("/api/tableros").json()["tableros"] == []
