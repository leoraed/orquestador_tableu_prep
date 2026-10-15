"""Linaje entre flows y páginas del catálogo (catálogo SQLite en memoria)."""
from types import SimpleNamespace as N

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.api as api
from conftest import iniciar_sesion
from src.catalog.db import BaseCatalog, get_catalog_db
from src.catalog.lineage import capa, construir_indice, norm_tabla
from src.catalog.models import (
    CatalogField, CatalogFlow, CatalogOutput, CatalogSource, CatalogTransformation,
)
from src.catalog.models_semantic import SemanticEntity, SemanticModel


# ── lineage ──────────────────────────────────────────────────────────────────

def test_capa_y_normalizacion():
    assert capa("stagedata_03_marketing_link") == "stagedata_03"
    assert capa("rawdata_mysql_orbith_contacto") == "rawdata"
    assert capa("STG01_CALENDARIO") == "otros"
    assert norm_tabla("[rawdata_mysql_orbith_contacto_historia]") == "rawdata_mysql_orbith_contacto_historia"
    assert norm_tabla("`orbith_bi`.`Tabla`") == "tabla"
    assert norm_tabla(None) is None


def _flow(name, sources, outputs):
    return N(flow_name=name,
             sources=[N(source_table=t, node_name=n, file_path=p) for t, n, p in sources],
             outputs=[N(destination_table=o) for o in outputs])


def test_indice_cruza_outputs_con_fuentes():
    flows = [
        _flow("rawdata_mysql_orbith_contacto",
              [("contacto", "contacto 2", None)],
              ["rawdata_mysql_orbith_contacto", "[rawdata_mysql_orbith_contacto_historia]"]),
        _flow("stagedata_01_comercial_contacto",
              [("rawdata_mysql_orbith_contacto", "rawdata_mysql_orbith_contacto 2", None),
               # consulta personalizada: sin source_table, se infiere por el nodo
               (None, "rawdata_mysql_orbith_contacto_historia", None),
               # nodo arbitrario sin tabla conocida: no se inventa relación
               (None, "Consulta rara", None),
               (None, "Plan 2024", "C:\\Input\\Plan 2024.xlsx")],
              ["stagedata_comercial_contacto"]),
    ]
    indice, fuentes, escritas = construir_indice(flows)

    assert indice.escriben["rawdata_mysql_orbith_contacto_historia"] == ["rawdata_mysql_orbith_contacto"]
    assert indice.leen["rawdata_mysql_orbith_contacto"] == ["stagedata_01_comercial_contacto"]
    assert indice.leen["rawdata_mysql_orbith_contacto_historia"] == ["stagedata_01_comercial_contacto"]
    assert escritas["rawdata_mysql_orbith_contacto"] == [
        "rawdata_mysql_orbith_contacto", "rawdata_mysql_orbith_contacto_historia"]

    refs = {r.etiqueta: r for r in fuentes["stagedata_01_comercial_contacto"]}
    assert refs["rawdata_mysql_orbith_contacto_historia"].inferida
    assert refs["Consulta rara"].tabla is None
    assert refs["Plan 2024.xlsx"].tabla is None


# ── páginas ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    BaseCatalog.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    raw = CatalogFlow(flow_name="rawdata_mysql_orbith_contacto", file_path="raw.tfl",
                      graph_data={"nodes": [{"id": "n1", "name": "contacto", "type": "input", "details": {}}],
                                  "edges": []})
    stg = CatalogFlow(flow_name="stagedata_03_marketing_link", file_path="stg.tfl")
    db.add_all([raw, stg]); db.flush()
    s1 = CatalogSource(flow_id=raw.id, node_id="a", node_name="contacto", source_type="mysql", source_table="contacto")
    s2 = CatalogSource(flow_id=stg.id, node_id="b", node_name="rawdata_mysql_orbith_contacto",
                       source_type="mysql", source_table="rawdata_mysql_orbith_contacto")
    db.add_all([s1, s2]); db.flush()
    db.add_all([
        CatalogOutput(flow_id=raw.id, node_id="o1", node_name="Salida", destination_table="rawdata_mysql_orbith_contacto"),
        CatalogOutput(flow_id=stg.id, node_id="o2", node_name="Salida", destination_table="stagedata_marketing_link"),
        CatalogField(source_id=s2.id, flow_id=stg.id, field_name="id_direccion", field_type="integer", ordinal=1,
                     description="Dirección del cliente"),
        CatalogField(source_id=s2.id, flow_id=stg.id, field_name="cliente_id", field_type="integer", ordinal=2),
        CatalogTransformation(flow_id=stg.id, node_id="t1", node_name="Join contactos", transform_type="join",
                              details={"join_type": "left", "conditions": []}),
    ])
    m = SemanticModel(name="modelo_dashboard_marketing"); db.add(m); db.flush()
    db.add(SemanticEntity(model_id=m.id, name="Link", base_table="stagedata_marketing_link", joins=[]))
    db.commit(); db.close()

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    api.app.dependency_overrides[get_catalog_db] = override
    yield iniciar_sesion(TestClient(api.app), Session, monkeypatch)
    api.app.dependency_overrides.clear()


def _datos(html: str) -> str:
    return html.split("const D = ", 1)[1].split(";\n", 1)[0]


def test_inicio_agrupa_por_capa_y_cruza_tablas(client):
    import json
    d = json.loads(_datos(client.get("/catalogo").text))
    flows = {f["name"]: f for f in d["flows"]}
    assert flows["stagedata_03_marketing_link"]["capa"] == "stagedata_03"
    assert flows["stagedata_03_marketing_link"]["lee"] == ["rawdata_mysql_orbith_contacto"]
    tablas = {t["tabla"]: t for t in d["tablas"]}
    assert tablas["rawdata_mysql_orbith_contacto"]["escriben"] == ["rawdata_mysql_orbith_contacto"]
    assert tablas["rawdata_mysql_orbith_contacto"]["leen"] == ["stagedata_03_marketing_link"]
    assert tablas["stagedata_marketing_link"]["modelos"] == ["modelo_dashboard_marketing"]
    assert d["stats"] == {"flows": 2, "tablas": 3, "campos": 2, "documentados": 1}


def test_buscar_campos_por_nombre_y_descripcion(client):
    r = client.get("/api/catalog/buscar-campos", params={"q": "direcc"}).json()
    assert r["total"] == 1 and r["campos"][0]["flow"] == "stagedata_03_marketing_link"
    assert client.get("/api/catalog/buscar-campos", params={"q": "cliente"}).json()["total"] == 2
    assert client.get("/api/catalog/buscar-campos", params={"q": "x"}).json()["total"] == 0


def test_detalle_flow_muestra_productores_lectores_y_modelos(client):
    import json
    d = json.loads(_datos(client.get("/catalogo/stagedata_03_marketing_link").text))
    assert d["lee"][0]["productores"] == ["rawdata_mysql_orbith_contacto"]
    assert d["escribe"][0]["modelos"] == ["modelo_dashboard_marketing"]
    assert [f["name"] for f in d["sources"][0]["fields"]] == ["id_direccion", "cliente_id"]
    assert d["transforms"][0]["type"] == "join"
    assert client.get("/catalogo/no_existe").status_code == 404
