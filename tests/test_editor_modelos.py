"""
Tests de los endpoints del editor de modelos, con un catálogo SQLite en memoria.
El DW no se toca: consultar() se reemplaza con un fake.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.api as api
from src.catalog import models as _catalog_models  # noqa: F401  registra tablas del catálogo
from src.catalog.db import BaseCatalog, get_catalog_db
from src.catalog.models_semantic import (
    SemanticDimension, SemanticEntity, SemanticMetric, SemanticModel,
)

MOD = "/api/modelos/mkt"


@pytest.fixture
def client():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    BaseCatalog.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    db = Session()
    m = SemanticModel(name="mkt")
    db.add(m)
    db.flush()
    e = SemanticEntity(model_id=m.id, name="Link", base_table="stagedata_marketing_link", joins=[])
    db.add(e)
    db.flush()
    db.add_all([
        SemanticDimension(entity_id=e.id, name="Fecha", expression="Fecha", data_type="date"),
        SemanticDimension(entity_id=e.id, name="Source", expression="Source", data_type="string"),
        SemanticDimension(entity_id=e.id, name="cond_OrbithHome",
                          expression="[cliente_id]=73", data_type="boolean"),
        SemanticMetric(entity_id=e.id, name="Ventas",
                       expression="COUNT(DISTINCT CASE WHEN Source = 'Ventas' THEN id_direccion END)"),
        SemanticMetric(entity_id=e.id, name="Mudanzas",
                       expression="COUNTD(IF [Source]='Mudanzas' and [cond_OrbithHome] THEN [id_direccion] end)"),
    ])
    db.commit()
    db.close()

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    api.app.dependency_overrides[get_catalog_db] = override
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def _campo(client, name):
    campos = client.get(f"{MOD}/campos").json()["campos"]
    return next(c for c in campos if c["name"] == name)


def test_listado_marca_estado_y_formula_original(client):
    mud = _campo(client, "Mudanzas")
    assert mud["estado"] == "pendiente"
    assert mud["tableau_formula"].startswith("COUNTD(IF")
    assert _campo(client, "Ventas")["estado"] == "sql"


def test_convertir_resuelve_contra_campos_del_modelo(client):
    mud = _campo(client, "Mudanzas")
    r = client.post(f"{MOD}/convertir", json={
        "formula": mud["tableau_formula"], "kind": "metric", "name": "Mudanzas"}).json()
    assert r["ok"], r
    # [Source] usa la expresión de la dimensión Source; [cond_OrbithHome] también
    # estaba en Tableau y se convierte en cadena; [id_direccion] no es campo → columna
    assert r["sql"] == (
        "COUNT(DISTINCT CASE WHEN Source='Mudanzas' AND (`cliente_id`=73) "
        "THEN `id_direccion` END)"
    )


def test_convertir_informa_lo_no_traducible(client):
    r = client.post(f"{MOD}/convertir", json={
        "formula": "COUNTD(IF [Calculation_354095557224935424] THEN [id_direccion] END)"}).json()
    assert not r["ok"]
    assert "no está en el modelo" in r["error"]


def test_editar_campo_valida_y_guarda(client):
    mud = _campo(client, "Mudanzas")
    url = f"{MOD}/campos/metric/{mud['id']}"

    # Cambiar solo la descripción de un campo pendiente está permitido
    r = client.put(url, json={**mud, "description": "Mudanzas de clientes"}).json()
    assert r["ok"] and r["campo"]["estado"] == "pendiente"

    # Escribir sintaxis Tableau nueva se rechaza
    r = client.put(url, json={**mud, "expression": "COUNTD([id_direccion])"}).json()
    assert not r["ok"] and "sintaxis Tableau" in r["error"]

    # SQL válido se guarda y conserva la fórmula original
    sql = "COUNT(DISTINCT CASE WHEN Source = 'Mudanzas' THEN id_direccion END)"
    r = client.put(url, json={**mud, "expression": sql}).json()
    assert r["ok"] and r["campo"]["estado"] == "sql"
    assert r["campo"]["tableau_formula"] == mud["tableau_formula"]  # no se pierde
    assert _campo(client, "Ventas")["estado"] == "sql"  # los demás campos intactos


def test_crear_campo_rechaza_duplicados(client):
    ent = client.get(f"{MOD}/campos").json()["campos"][0]["entity_id"]
    base = {"kind": "metric", "entity_id": ent, "expression": "COUNT(*)", "format": "number"}
    assert not client.post(f"{MOD}/campos", json={**base, "name": "Ventas"}).json()["ok"]
    r = client.post(f"{MOD}/campos", json={**base, "name": "Filas"}).json()
    assert r["ok"] and r["campo"]["id"]

    assert client.delete(f"{MOD}/campos/metric/{r['campo']['id']}").json()["ok"]
    assert all(c["name"] != "Filas" for c in client.get(f"{MOD}/campos").json()["campos"])


def test_probar_pasa_la_expresion_sin_guardar(client, monkeypatch):
    llamadas = {}

    def fake(modelo, metricas, dimensiones, **kw):
        llamadas.update(modelo=modelo, metricas=metricas, dimensiones=dimensiones, **kw)
        return {"sql": "SELECT …", "columnas": metricas, "filas": [[12]], "total": 1,
                "tiempo_ms": 5, "error": None}

    monkeypatch.setattr(api, "_consultar_dw", fake)
    r = client.post(f"{MOD}/probar", json={
        "kind": "metric", "name": "Mudanzas", "expression": "COUNT(*)",
        "periodo": {"campo": "Fecha", "desde": "2026-01", "hasta": "2026-01"},
        "filtros": {"cliente_id": "73", "vacío": ""},
    }).json()
    assert r["ok"] and r["filas"] == [[12]]
    assert llamadas["overrides"] == {"metricas": {"Mudanzas": {"expression": "COUNT(*)"}}}
    assert llamadas["filtros"] == {"cliente_id": "73"}
    assert llamadas["limite"] == 1


def test_entidad_se_edita_sin_tocar_campos(client):
    ent_id = client.get(f"{MOD}/campos").json()["campos"][0]["entity_id"]
    r = client.put(f"{MOD}/entidades/{ent_id}", json={
        "name": "Link", "base_table": "stagedata_marketing_link",
        "joins": [{"table": "t2", "on": "t2.id = stagedata_marketing_link.id", "type": "inner"}],
    }).json()
    assert r["ok"] and r["entidad"]["joins"][0]["type"] == "INNER"
    assert len(client.get(f"{MOD}/campos").json()["campos"]) == 5


def test_pagina_editor_renderiza(client):
    html = client.get("/modelos/mkt").text
    assert "Expresión SQL" in html and '"campos"' in html
