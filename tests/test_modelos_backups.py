"""Reimportar sin pisar lo editado, backups y restauración (catálogo SQLite en memoria)."""
import io

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.api as api
from conftest import iniciar_sesion
from src.catalog import models as _catalog_models  # noqa: F401  registra tablas del catálogo
from src.catalog.db import BaseCatalog, get_catalog_db

MOD = "/api/modelos/modelo_dashboard_marketing"

TWB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook><datasources>
  <datasource name='federated.ds1' caption='Modelo_Dashboard_Marketing'>
    <connection class='federated'>
      <relation type='table' name='link' table='[stagedata_marketing_link]'/>
      <relation type='table' name='contacto' table='[stagedata_comercial_contacto]'/>
      <cols>
        <map key='[Source]' value='[stagedata_marketing_link].[Source]'/>
        <map key='[id_direccion]' value='[stagedata_marketing_link].[id_direccion]'/>
        <map key='[cliente_id]' value='[stagedata_marketing_link].[cliente_id]'/>
      </cols>
    </connection>
    <column name='[Source]' role='dimension' datatype='string'/>
    <column name='[cliente_id]' caption='Cliente Id' role='dimension' datatype='integer'/>
    <column name='[Calculation_1]' caption='Ventas' role='measure' datatype='integer'>
      <calculation class='tableau' formula="COUNTD(IF [Source]='Ventas' THEN [id_direccion] END)"/>
    </column>
    <column name='[Calculation_2]' caption='Leads' role='measure' datatype='integer'>
      <calculation class='tableau' formula="COUNTD(IF [Source]='Leads' THEN [id_direccion] END)"/>
    </column>
  </datasource>
</datasources></workbook>
"""


def _twb(**reemplazos) -> bytes:
    t = TWB
    for a, b in reemplazos.items():
        t = t.replace(a, b)
    return t.encode("utf-8")


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


def importar(client, contenido: bytes, modo="aplicar"):
    return client.post("/api/modelos/importar-twb", data={"modo": modo},
                       files={"file": ("dash.twb", io.BytesIO(contenido))}).json()


def campos(client):
    return {c["name"]: c for c in client.get(f"{MOD}/campos").json()["campos"]}


def test_importar_crea_sin_joins_cartesianos_y_avisa(client):
    r = importar(client, _twb())
    [m] = r["modelos"]
    assert m["nuevo"] and m["metricas"] == 2
    assert any("stagedata_comercial_contacto" in a for a in m["avisos"])
    html = client.get("/modelos/modelo_dashboard_marketing").text
    assert '"joins": []' in html


def test_reimportar_respeta_lo_editado(client):
    importar(client, _twb())
    c = campos(client)
    ent = c["Ventas"]["entity_id"]
    # Edición manual de Ventas + tabla base/joins definidos a mano
    client.put(f"{MOD}/campos/metric/{c['Ventas']['id']}",
               json={**c["Ventas"], "expression": "COUNT(DISTINCT `id_direccion`)"})
    client.put(f"{MOD}/entidades/{ent}", json={
        "name": "Modelo_Dashboard_Marketing", "base_table": "stagedata_marketing_link",
        "joins": [{"table": "t2", "on": "t2.id = stagedata_marketing_link.id_direccion", "type": "LEFT"}]})

    # El workbook cambió: Ventas y Leads con otra fórmula, Cliente Id desaparece, aparece Mudanzas
    nuevo = _twb(**{
        "'Ventas' THEN": "'Venta' THEN", "'Leads' THEN": "'Lead' THEN",
        "<column name='[cliente_id]' caption='Cliente Id' role='dimension' datatype='integer'/>": "",
        "</datasource>": """<column name='[Calculation_3]' caption='Mudanzas' role='measure' datatype='integer'>
          <calculation class='tableau' formula="COUNTD(IF [Source]='Mudanzas' THEN [id_direccion] END)"/>
        </column></datasource>""",
    })

    # Preview: no escribe nada
    plan = importar(client, nuevo, modo="preview")["modelos"][0]["plan"]
    assert [x["name"] for x in plan["actualizados"]] == ["Leads"]
    assert [x["name"] for x in plan["agregados"]] == ["Mudanzas"]
    assert [x["name"] for x in plan["eliminados"]] == ["Cliente Id"]
    assert [(x["name"], x["motivo"]) for x in plan["conservados"]] == [("Ventas", "editado a mano")]
    assert "Mudanzas" not in campos(client)

    importar(client, nuevo)
    c = campos(client)
    assert c["Ventas"]["expression"] == "COUNT(DISTINCT `id_direccion`)"   # conservada
    assert "'Lead'" in c["Leads"]["expression"]                            # actualizada
    assert "Mudanzas" in c and "Cliente Id" not in c
    html = client.get("/modelos/modelo_dashboard_marketing").text
    assert "t2.id = stagedata_marketing_link.id_direccion" in html          # joins intactos

    # Quedó un backup automático de antes de reimportar
    motivos = [b["motivo"] for b in client.get(f"{MOD}/backups").json()["backups"]]
    assert "Antes de reimportar dash.twb" in motivos


def test_campos_sin_marca_se_protegen(client):
    """Modelos anteriores a la marca 'editado' (None): reimportar no los pisa."""
    importar(client, _twb())
    s = next(api.app.dependency_overrides[get_catalog_db]())
    from src.catalog.models_semantic import SemanticMetric
    for m in s.query(SemanticMetric).all():
        m.editado = None
    s.commit()
    plan = importar(client, _twb(**{"'Ventas' THEN": "'Venta' THEN"}), modo="preview")["modelos"][0]["plan"]
    assert plan["actualizados"] == []
    assert plan["conservados"][0]["motivo"].startswith("sin marca")


def test_backup_manual_restaurar_y_descargar(client):
    importar(client, _twb())
    b = client.post(f"{MOD}/backups", json={"motivo": "Antes de probar"}).json()["backup"]
    assert b["campos"] == 4 and not b["automatico"]

    c = campos(client)
    client.delete(f"{MOD}/campos/metric/{c['Leads']['id']}")
    assert "Leads" not in campos(client)

    assert client.post(f"/api/modelos/backups/{b['id']}/restaurar").json()["ok"]
    assert "Leads" in campos(client)
    # Restaurar guarda antes el estado que se pisa
    motivos = [x["motivo"] for x in client.get(f"{MOD}/backups").json()["backups"]]
    assert any(m.startswith("Antes de restaurar") for m in motivos)

    contenido = yaml.safe_load(client.get(f"/api/modelos/backups/{b['id']}/yaml").text)
    assert contenido["name"] == "modelo_dashboard_marketing"


def test_modelo_eliminado_se_recupera(client):
    importar(client, _twb())
    client.post(f"{MOD}/eliminar")
    eliminados = client.get("/api/modelos-eliminados").json()["modelos"]
    assert [m["model_name"] for m in eliminados] == ["modelo_dashboard_marketing"]
    assert client.post(f"/api/modelos/backups/{eliminados[0]['id']}/restaurar").json()["ok"]
    assert "Ventas" in campos(client)
    assert client.get("/api/modelos-eliminados").json()["modelos"] == []


def test_importar_yaml_crea_o_reemplaza(client):
    importar(client, _twb())
    texto = client.get(f"{MOD}/yaml").text
    contenido = yaml.safe_load(texto)
    contenido["entities"][0]["metrics"] = contenido["entities"][0]["metrics"][:1]

    r = client.post("/api/modelos/importar-yaml",
                    files={"file": ("m.yaml", io.BytesIO(yaml.dump(contenido).encode()))}).json()
    assert r["ok"] and r["reemplazado"]
    assert sum(c["kind"] == "metric" for c in campos(client).values()) == 1

    malo = client.post("/api/modelos/importar-yaml",
                       files={"file": ("x.yaml", io.BytesIO(b"hola: 1"))}).json()
    assert not malo["ok"] and "name" in malo["error"]


def test_renombrar_modelo_lleva_sus_backups(client):
    importar(client, _twb())
    client.post(f"{MOD}/backups", json={"motivo": "x"})
    client.put(MOD, json={"name": "marketing", "description": ""})
    assert len(client.get("/api/modelos/marketing/backups").json()["backups"]) == 1
    assert client.get("/api/modelos-eliminados").json()["modelos"] == []
