"""Herramientas del MCP para publicar tableros (catálogo SQLite en memoria)."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import mcp_server
from src.catalog.db import BaseCatalog
from src.catalog.models_semantic import SemanticModel

HTML = "<!DOCTYPE html><html><body><h1>NOC</h1></body></html>"


@pytest.fixture
def mcp(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    BaseCatalog.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add(SemanticModel(name="stagedata_noc_link_orbith_bi"))
    s.commit()
    s.close()
    monkeypatch.setattr(mcp_server, "SessionCatalog", Session)
    monkeypatch.setenv("ORQUESTADOR_URL", "http://10.0.10.44:8000/")
    marca = mcp_server._usuario_actual.set(mcp_server._acceso.Usuario("Ana", "usuario"))
    yield mcp_server
    mcp_server._usuario_actual.reset(marca)


def test_publicar_listar_y_obtener(mcp):
    r = mcp.publicar_dashboard("NOC · Actividad", HTML, descripcion="Actividad del NOC",
                               modelo="stagedata_noc_link_orbith_bi", tags="noc, operadores",
                               datos_hasta="2026-10-13")
    assert "Link: http://10.0.10.44:8000/tableros/noc-actividad" in r
    assert "Slug: noc-actividad" in r

    lista = mcp.listar_dashboards()
    assert "`noc-actividad`" in lista and "v1" in lista and "datos al 2026-10-13" in lista
    assert "No hay tableros" in mcp.listar_dashboards(modelo="otro")

    o = mcp.obtener_dashboard("noc-actividad")
    assert "versión 1 de 1" in o and HTML in o


def test_actualizar_publica_version_nueva(mcp):
    mcp.publicar_dashboard("NOC · Actividad", HTML)
    r = mcp.publicar_dashboard("", "<html>v2</html>", actualizar="noc-actividad", datos_hasta="2026-11-30",
                               nota="datos a noviembre")
    assert "versión 2" in r
    assert "<html>v2</html>" in mcp.obtener_dashboard("noc-actividad")
    assert HTML in mcp.obtener_dashboard("noc-actividad", version=1)
    # El título se conserva si no se pasa uno nuevo
    assert "**NOC · Actividad**" in mcp.listar_dashboards()


def test_errores_claros(mcp):
    assert "no existe" in mcp.publicar_dashboard("X", HTML, modelo="modelo_inventado")
    assert "no existe un tablero" in mcp.publicar_dashboard("X", HTML, actualizar="nada")
    assert "no parece HTML" in mcp.publicar_dashboard("X", "solo texto")
    assert "AAAA-MM-DD" in mcp.publicar_dashboard("X", HTML, datos_hasta="13/10/2026")
    assert "título es obligatorio" in mcp.publicar_dashboard("", HTML)
    assert "no existe un tablero" in mcp.obtener_dashboard("nada")
    # Ningún error dejó tableros a medio crear
    assert "No hay tableros" in mcp.listar_dashboards()
