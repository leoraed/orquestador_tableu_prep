"""Importar fuentes con modelo de relaciones y consultas SQL personalizadas."""
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.api as api
from conftest import iniciar_sesion
from src.catalog import models as _catalog_models  # noqa: F401  registra tablas del catálogo
from src.catalog import twb_relaciones as rel
from src.catalog.db import BaseCatalog, get_catalog_db
from src.catalog.sql_compiler import _from_sql
from src.catalog.twb_parser import parse_twb


def _meta(local, padre, remota, tipo):
    return (f"<metadata-record class='column'><remote-name>{remota}</remote-name><local-name>{local}</local-name>"
            f"<parent-name>[{padre}]</parent-name><local-type>{tipo}</local-type></metadata-record>")


# Ventas (hechos, SQL de oss_prd) — Cliente (tabla) y Calendario (tabla, multiplica filas);
# Plan (hechos, SQL) se relaciona con Ventas por fecha y cliente.
TWB = f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook>
<datasources>
  <datasource name='federated.ds1' caption='Comercial'>
    <connection class='federated'>
      <named-connections>
        <named-connection name='mysql.oss'><connection class='mysql' dbname='oss_prd' server='x'/></named-connection>
        <named-connection name='mysql.bi'><connection class='mysql' dbname='orbith_bi' server='x'/></named-connection>
      </named-connections>
      <metadata-records>
        {_meta('[Fecha]', 'Custom SQL Query', 'Fecha', 'datetime')}
        {_meta('[cliente_id]', 'Custom SQL Query', 'cliente_id', 'integer')}
        {_meta('[monto]', 'Custom SQL Query', 'monto', 'real')}
        {_meta('[id]', 'Custom SQL Query4', 'id', 'integer')}
        {_meta('[segmento]', 'Custom SQL Query4', 'segmento', 'string')}
        {_meta('[Fecha (Custom SQL Query11)]', 'Custom SQL Query11', 'Fecha', 'date')}
        {_meta('[cliente_id (Custom SQL Query11)]', 'Custom SQL Query11', 'cliente_id', 'integer')}
        {_meta('[plan]', 'Custom SQL Query11', 'plan', 'real')}
        {_meta('[FechaCal]', 'm_calendario', 'FechaCal', 'date')}
        {_meta('[Periodo]', 'm_calendario', 'Periodo', 'string')}
      </metadata-records>
    </connection>
    <column name='[Calculation_1]' caption='Ventas' role='measure' datatype='real'>
      <calculation class='tableau' formula='SUM([monto])'/>
    </column>
    <column name='[Calculation_2]' caption='Plan Ventas' role='measure' datatype='real'>
      <calculation class='tableau' formula='SUM([plan])'/>
    </column>
    <column name='[Calculation_3]' caption='Cumplimiento' role='measure' datatype='real'>
      <calculation class='tableau' formula='SUM([monto]) / SUM([plan])'/>
    </column>
    <column name='[segmento]' caption='Segmento' role='dimension' datatype='string'/>
    <column name='[Periodo]' role='dimension' datatype='string'/>
    <object-graph>
      <objects>
        <object caption='Ventas' id='_V'><properties context=''>
          <relation connection='mysql.oss' name='Custom SQL Query' type='text'>select Fecha, cliente_id, monto
from ventas v
where v.monto &gt;&gt;= 0 and TRIM(LEADING 'x' FROM v.codigo) &lt;&gt; 'from y'</relation>
        </properties></object>
        <object caption='Cliente' id='_C'><properties context=''>
          <relation connection='mysql.oss' name='Custom SQL Query4' type='text'>select id, segmento from oss_prd.cliente</relation>
        </properties></object>
        <object caption='Plan' id='_P'><properties context=''>
          <relation connection='mysql.oss' name='Custom SQL Query11' type='text'>select * from orbith_bi.plan</relation>
        </properties></object>
        <object caption='Calendario' id='_K'><properties context=''>
          <relation connection='mysql.bi' name='m_calendario' table='[m_calendario]' type='table'/>
        </properties></object>
      </objects>
      <relationships>
        <relationship>
          <expression op='='><expression op='[cliente_id]'/><expression op='[id]'/></expression>
          <first-end-point object-id='_V'/><second-end-point object-id='_C'/>
        </relationship>
        <relationship>
          <expression op='AND'>
            <expression op='='><expression op='date([Fecha])'/><expression op='[Fecha (Custom SQL Query11)]'/></expression>
            <expression op='='><expression op='[cliente_id]'/><expression op='[cliente_id (Custom SQL Query11)]'/></expression>
          </expression>
          <first-end-point object-id='_V'/><second-end-point object-id='_P'/>
        </relationship>
        <relationship>
          <expression op='='><expression op='[FechaCal]'/><expression op='date([Fecha])'/></expression>
          <first-end-point object-id='_K'/><second-end-point object-id='_V'/>
        </relationship>
      </relationships>
    </object-graph>
  </datasource>
</datasources>
<worksheets><worksheet name='Hoja'><table><view>
  <datasource-dependencies datasource='federated.ds1'>
    <column name='[Calculation_1]'/><column name='[Calculation_2]'/><column name='[Calculation_3]'/>
    <column name='[segmento]'/><column name='[Periodo]'/>
  </datasource-dependencies>
</view></table></worksheet></worksheets>
</workbook>
""".encode("utf-8")


def _por_nombre(modelos):
    return {m.model_name: m for m in modelos}


def test_un_modelo_por_tabla_de_hechos():
    ms = _por_nombre(parse_twb(TWB))
    assert set(ms) == {"comercial_ventas", "comercial_plan"}
    v, p = ms["comercial_ventas"], ms["comercial_plan"]

    # Origen: el SQL desescapado y con la base del .twb en las tablas sin calificar
    assert v.base_table == "Ventas"
    assert "from `oss_prd`.`ventas` v" in v.base_sql
    assert "monto >= 0" in v.base_sql and "FROM v.codigo" in v.base_sql and "'from y'" in v.base_sql
    assert p.base_sql == "select * from orbith_bi.plan"

    # Joins con su condición; el plan llega a Cliente y Calendario con sus propias claves
    on_v = {j["alias"]: j["on"] for j in v.joins}
    assert on_v == {"Cliente": "`Ventas`.`cliente_id` = `Cliente`.`id`",
                    "Calendario": "`Calendario`.`FechaCal` = DATE(`Ventas`.`Fecha`)"}
    on_p = {j["alias"]: j["on"] for j in p.joins}
    assert on_p == {"Cliente": "`Plan`.`cliente_id` = `Cliente`.`id`",
                    "Calendario": "`Plan`.`Fecha` = `Calendario`.`FechaCal`"}
    assert next(j for j in v.joins if j["alias"] == "Calendario")["table"] == "m_calendario"
    assert "sql" in next(j for j in v.joins if j["alias"] == "Cliente")

    # Campos: cada métrica en su hecho, las dimensiones compartidas en los dos, Fecha común
    for m, metrica, fecha in ((v, "Ventas", "`Ventas`.`Fecha`"), (p, "Plan Ventas", "`Plan`.`Fecha`")):
        assert [x["name"] for x in m.metrics] == [metrica]
        dims = {x["name"]: x["expression"] for x in m.dimensions}
        assert dims["Segmento"] == "`Cliente`.`segmento`" and dims["Periodo"] == "`Calendario`.`Periodo`"
        assert dims["Fecha"] == fecha
    assert any("Cumplimiento" in a and "consulta por modelo" in a for a in v.avisos)
    assert any("se agregó «oss_prd.» a ventas" in a for a in v.avisos)
    assert [(t["nombre"], t["hecho"]) for t in v.tablas] == [
        ("Ventas", True), ("Cliente", False), ("Plan", True), ("Calendario", False)]


def test_elegir_los_hechos_a_mano():
    [m] = parse_twb(TWB, hechos={"Comercial": ["Ventas"]})
    assert m.model_name == "comercial_ventas"
    on = {j["alias"]: j["on"] for j in m.joins}
    assert on["Plan"] == "DATE(`Ventas`.`Fecha`) = `Plan`.`Fecha` AND `Ventas`.`cliente_id` = `Plan`.`cliente_id`"
    assert {x["name"] for x in m.metrics} == {"Ventas", "Plan Ventas", "Cumplimiento"}


def test_calificar_tablas():
    sql, tablas = rel.calificar_tablas(
        "WITH t AS (select 1) SELECT * FROM t JOIN `a` ON 1 LEFT JOIN db.b ON 1 "
        "WHERE EXTRACT(YEAR FROM fecha) = 1 AND x = 'from c' -- from d\n", "oss")
    assert tablas == ["a"]
    assert "JOIN `oss`.`a` ON" in sql and "FROM t JOIN" in sql and "db.b" in sql and "'from c'" in sql
    assert rel.desescapar_sql("a <<= b and c >> d and <[Parameters].[P 1]>") == "a <= b and c > d and <[Parameters].[P 1]>"


def test_from_sql_subconsultas_y_joins_que_se_usan():
    e = SimpleNamespace(base_table="Ventas", base_sql="select * from v -- fin", joins=[
        {"table": "Cliente", "alias": "Cliente", "on": "`Ventas`.`c` = `Cliente`.`id`", "type": "LEFT", "sql": "select 1 as id"},
        {"table": "Canal", "alias": "Canal", "on": "`Cliente`.`k` = `Canal`.`id`", "type": "LEFT", "sql": "select 2 as id"},
        {"table": "m_calendario", "alias": "Calendario", "on": "`Calendario`.`f` = `Ventas`.`f`", "type": "LEFT"},
    ])
    todo = _from_sql(e)
    assert todo.startswith("FROM (\nselect * from v -- fin\n) AS `Ventas`")
    assert "LEFT JOIN `m_calendario` AS `Calendario`" in todo
    solo_base = _from_sql(e, "SUM(`Ventas`.`monto`)")
    assert "JOIN" not in solo_base
    canal = _from_sql(e, "`Canal`.`nombre`")                       # Canal necesita a Cliente
    assert "AS `Cliente`" in canal and "AS `Canal`" in canal and "Calendario" not in canal
    # Modelos sin SQL (columnas posiblemente sin calificar): se mantienen todos los joins
    clasico = SimpleNamespace(base_table="t", base_sql=None, joins=[{"table": "u", "alias": "u", "on": "t.a = u.a"}])
    assert "JOIN `u`" in _from_sql(clasico, "SUM(monto)")


def test_fuente_comun_con_una_consulta_personalizada():
    twb = b"""<workbook><datasources><datasource name='ds' caption='Simple'>
      <connection class='mysql' dbname='orbith_bi'>
        <relation name='Custom SQL Query' type='text'>select a, b from t where a &lt;&lt; 3</relation>
        <cols><map key='[a]' value='[Custom SQL Query].[a]'/></cols>
      </connection>
      <column name='[a]' caption='A' role='dimension' datatype='integer'/>
    </datasource></datasources></workbook>"""
    [m] = parse_twb(twb)
    assert m.base_table == "Custom SQL Query" and m.base_sql == "select a, b from t where a < 3"
    assert m.dimensions[0]["expression"] == "`a`"


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


def test_importar_desde_la_app(client):
    importar = lambda modo, hechos="": client.post(
        "/api/modelos/importar-twb", data={"modo": modo, "hechos": hechos},
        files={"file": ("c.twb", io.BytesIO(TWB))}).json()
    prev = importar("preview")
    assert prev["preview"] and {m["name"] for m in prev["modelos"]} == {"comercial_ventas", "comercial_plan"}
    assert prev["modelos"][0]["grupo"] == "Comercial" and prev["modelos"][0]["origen_sql"]
    solo = importar("preview", '{"Comercial": ["Ventas"]}')
    assert [m["name"] for m in solo["modelos"]] == ["comercial_ventas"]

    assert importar("aplicar")["ok"]
    html = client.get("/modelos/comercial_plan").text
    assert "select * from orbith_bi.plan" in html and "`Plan`.`cliente_id` = `Cliente`.`id`" in html
    # El export conserva el SQL de origen
    assert "base_sql" in client.get("/api/modelos/comercial_plan/yaml").text
