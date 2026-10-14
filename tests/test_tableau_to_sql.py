"""
Tests del conversor Tableau → SQL. Las fórmulas son reales del
workbook Modelo_Dashboard_Marketing.
"""
import pytest

from src.catalog.tableau_to_sql import NoTraducible, make_resolver, translate_formula
from src.catalog.twb_parser import parse_twb

BASE = "stagedata_marketing_link"

COLUMNS = {
    "[Source]": (BASE, "Source"),
    "[id_direccion]": (BASE, "id_direccion"),
    "[cliente_id]": (BASE, "cliente_id"),
    "[Fecha]": (BASE, "Fecha"),
    "[direccion_id]": ("stagedata_marketing_ventas_anuales", "direccion_id"),
}

DEFS = {
    "[Source]": {"formula": ""},
    # cond_OrbithHome
    "[Calculation_1]": {"formula": "[cliente_id]=73"},
    # cálculo que referencia a otro cálculo
    "[Calculation_2]": {"formula": "[Calculation_1] and [Source]='Ventas'"},
    "[Calculation_ciclo_a]": {"formula": "[Calculation_ciclo_b] + 1"},
    "[Calculation_ciclo_b]": {"formula": "[Calculation_ciclo_a] + 1"},
}


def tr(formula: str) -> str:
    resolve = make_resolver(DEFS, COLUMNS, BASE, {"federated.ds1", "federated.ds2"}, "federated.ds1")
    return translate_formula(formula, resolve)


# ── sintaxis ─────────────────────────────────────────────────────────────────

def test_countd_if_con_referencia_a_calculo():
    sql = tr("COUNTD(IF [Source]='Mudanzas' and [Calculation_1] THEN [id_direccion] end)")
    assert sql == (
        "COUNT(DISTINCT CASE WHEN `Source`='Mudanzas' AND (`cliente_id`=73) "
        "THEN `id_direccion` END)"
    )


def test_referencias_anidadas_se_resuelven_recursivamente():
    assert tr("COUNTD(IF [Calculation_2] THEN [id_direccion] END)") == (
        "COUNT(DISTINCT CASE WHEN ((`cliente_id`=73) AND `Source`='Ventas') "
        "THEN `id_direccion` END)"
    )


def test_elseif():
    assert tr("IF [cliente_id]=1 THEN 'a' ELSEIF [cliente_id]=2 THEN 'b' ELSE 'c' END") == (
        "CASE WHEN `cliente_id`=1 THEN 'a' WHEN `cliente_id`=2 THEN 'b' ELSE 'c' END"
    )


def test_iif_y_avg():
    assert tr("IIF(AVG([id_direccion])>=AVG([cliente_id]),1,-1)") == (
        "CASE WHEN AVG(`id_direccion`)>=AVG(`cliente_id`) THEN 1 ELSE -1 END"
    )


def test_not_isnull_columna_de_otra_tabla():
    assert tr("not(ISNULL([direccion_id]))") == (
        "NOT((`stagedata_marketing_ventas_anuales`.`direccion_id` IS NULL))"
    )


def test_zn_y_comentarios():
    assert tr("ZN([id_direccion]) // comentario [cond_MTD]") == "COALESCE(`id_direccion`, 0)"


def test_fechas():
    assert tr("date(DATEADD('day',-1,today()))") == "DATE(DATE_ADD(CURDATE(), INTERVAL -1 DAY))"
    assert tr("date(DATETRUNC('month',today()))") == (
        "DATE(DATE_SUB(DATE(CURDATE()), INTERVAL DAYOFMONTH(CURDATE()) - 1 DAY))"
    )
    assert tr("DATEPART('weekday', [Fecha])=1") == "DAYOFWEEK(`Fecha`)=1"
    assert tr("[Fecha] >= #2024-01-01#") == "`Fecha` >= '2024-01-01'"


def test_strings_con_comillas_dobles():
    assert tr('[Source] == "Ventas"') == "`Source` = 'Ventas'"
    assert tr("[Source] = 'O''Higgins'") == "`Source` = 'O''Higgins'"


def test_columna_sin_mapa_y_tabla_explicita():
    assert tr("SUM([Inversión])") == "SUM(`Inversión`)"
    assert tr("[otra_tabla].[col]") == "`otra_tabla`.`col`"


# ── no traducibles ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("formula, motivo", [
    ("sum({ INCLUDE [Fecha] : COUNTD([id_direccion]) })", "LOD"),
    ("{ FIXED :MONTH(MAX([Fecha]))}", "LOD"),
    ("[id_direccion] / TOTAL([id_direccion])", "cálculo de tabla"),
    ("WINDOW_SUM(SUM([id_direccion]))", "cálculo de tabla"),
    ("YEAR([Fecha])=[Parameters].[Parameter 5]", "parámetro"),
    ("__tableau_internal_object_id__].[stagedata_link_66EB", "objeto interno"),
    ("[federated.ds2].[Ventas]", "otra fuente"),
    ("[Source] + ' x'", "concatenación"),
    ("DATEDIFF('month', [Fecha], [Fecha])", "DATEDIFF"),
    ("SPLIT([Source], '-', 1)", "función no soportada"),
    ("[Calculation_ciclo_a]", "circular"),
])
def test_no_traducibles(formula, motivo):
    with pytest.raises(NoTraducible, match=motivo):
        tr(formula)


# ── parser de punta a punta ──────────────────────────────────────────────────

TWB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook>
  <datasources>
    <datasource name='Parameters'>
      <column name='[Parameter 5]' caption='Año' role='measure' datatype='integer'/>
    </datasource>
    <datasource name='federated.ds1' caption='Modelo_Dashboard_Marketing'>
      <connection class='federated'>
        <relation type='table' name='link' table='[stagedata_marketing_link]'/>
        <cols>
          <map key='[Source]' value='[stagedata_marketing_link].[Source]'/>
          <map key='[id_direccion]' value='[stagedata_marketing_link].[id_direccion]'/>
          <map key='[cliente_id]' value='[stagedata_marketing_link].[cliente_id]'/>
          <map key='[Fecha]' value='[stagedata_marketing_link].[Fecha]'/>
        </cols>
      </connection>
      <column name='[Source]' role='dimension' datatype='string'/>
      <column name='[Fecha]' role='dimension' datatype='date'/>
      <column name='[Calculation_1]' caption='cond_OrbithHome' role='dimension' datatype='boolean'>
        <calculation class='tableau' formula='[cliente_id]=73'/>
      </column>
      <column name='[Calculation_2]' caption='Mudanzas' role='measure' datatype='integer'>
        <calculation class='tableau'
          formula="COUNTD(IF [Source]='Mudanzas' and [Calculation_1] THEN [id_direccion] end)"/>
      </column>
      <column name='[Calculation_3]' caption='Ventas CM' role='measure' datatype='integer'>
        <calculation class='tableau'
          formula="COUNTD(IF [Source]='Ventas' and YEAR([Fecha])=[Parameters].[Parameter 5] THEN [id_direccion] end)"/>
      </column>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Hoja 1'>
      <table>
        <view>
          <datasource-dependencies datasource='federated.ds1'>
            <column name='[Source]'/>
            <column name='[Fecha]'/>
            <column name='[Calculation_2]'/>
            <column name='[Calculation_3]'/>
          </datasource-dependencies>
        </view>
      </table>
    </worksheet>
  </worksheets>
</workbook>
"""


def test_parse_twb_traduce_y_reporta_pendientes():
    [ds] = parse_twb(TWB.encode("utf-8"))
    dims = {d["name"]: d["expression"] for d in ds.dimensions}
    mets = {m["name"]: m["expression"] for m in ds.metrics}

    assert dims == {"Source": "`Source`", "Fecha": "`Fecha`"}
    # Calculation_1 no está en ninguna hoja pero se resuelve igual
    assert mets["Mudanzas"] == (
        "COUNT(DISTINCT CASE WHEN `Source`='Mudanzas' AND (`cliente_id`=73) "
        "THEN `id_direccion` END)"
    )
    # Lo no traducible queda con la fórmula original y se reporta
    assert mets["Ventas CM"].startswith("COUNTD(IF [Source]='Ventas'")
    assert ds.pendientes == [
        {"name": "Ventas CM", "kind": "metric",
         "reason": "parámetro de Tableau ([Parameters].[Parameter 5])"},
    ]
