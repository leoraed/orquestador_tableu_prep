"""Tableros vivos: compilador (granos, IN, períodos relativos), recetas, ejecución, app y MCP."""
import io
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import mcp_server
import src.api as api
import src.catalog.sql_compiler as sc
from conftest import iniciar_sesion
from src.catalog import tableros_vivos as vivos
from src.catalog.db import BaseCatalog, get_catalog_db
from src.catalog.models_semantic import SemanticDimension, SemanticEntity, SemanticMetric, SemanticModel
from src.catalog.tableros_service import TableroError
from src.permisos import permisos_de

MODELO = "noc"
RECETA = {
    "modelo": MODELO,
    "filtros": [
        {"id": "periodo", "tipo": "periodo", "etiqueta": "Período", "campo": "Fecha", "default": "ultimos_12_meses"},
        {"id": "operador", "tipo": "dimension", "etiqueta": "Operador", "dimension": "Operador", "multiple": True},
    ],
    "consultas": {
        "mensual": {"metricas": ["cant_SR"], "dimensiones": ["Fecha:mes"],
                    "filtros": {"Operador": "$operador", "Source": "SR"}, "periodo": "$periodo"},
        "ranking": {"metricas": ["cant_SR"], "dimensiones": ["Operador"], "periodo": "$periodo"},
    },
}
HTML_VIVO = "<html><head></head><body><script>Tablero.alRecibirDatos(function (d) {})</script></body></html>"


@pytest.fixture
def Session(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    BaseCatalog.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    db = S()
    m = SemanticModel(name=MODELO); db.add(m); db.flush()
    e = SemanticEntity(model_id=m.id, name="Link", base_table="stagedata_noc_link", joins=[]); db.add(e); db.flush()
    db.add_all([
        SemanticDimension(entity_id=e.id, name="Fecha", expression="`fecha`", data_type="date"),
        SemanticDimension(entity_id=e.id, name="Operador", expression="`operador`", data_type="string"),
        SemanticDimension(entity_id=e.id, name="Source", expression="`source`", data_type="string"),
        SemanticMetric(entity_id=e.id, name="cant_SR", expression="COUNT(DISTINCT `sr_id`)"),
    ])
    db.commit(); db.close()
    monkeypatch.setattr(sc, "SessionCatalog", S)
    vivos._CACHE.clear()
    return S


class _ConnFalsa:
    """Captura el SQL que ejecutaría el compilador."""
    def __init__(self, filas=None):
        self.ejecutado, self.filas = [], filas or []

    def cursor(self):
        conn = self

        class C:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params): conn.ejecutado.append((sql, params))
            def fetchall(self): return conn.filas
        return C()

    def close(self): pass


# ── compilador ───────────────────────────────────────────────────────────────

def test_periodos_relativos():
    hoy = date(2026, 10, 19)
    assert sc.resolver_periodo_relativo("ultimos_12_meses", hoy) == ("2025-11-01", "2026-10-19")
    assert sc.resolver_periodo_relativo("ultimos_1_meses", hoy) == ("2026-10-01", "2026-10-19")
    assert sc.resolver_periodo_relativo("mes_anterior", hoy) == ("2026-09-01", "2026-09-30")
    assert sc.resolver_periodo_relativo("anio_anterior", hoy) == ("2025-01-01", "2025-12-31")
    assert sc.resolver_periodo_relativo("ultimos_7_dias", hoy) == ("2026-10-13", "2026-10-19")
    assert sc.resolver_periodo_relativo("mes_actual", date(2026, 1, 5)) == ("2026-01-01", "2026-01-05")
    with pytest.raises(sc.ConsultaError):
        sc.resolver_periodo_relativo("la_semana_que_viene", hoy)


def test_grano_in_relativo_distinct_y_orden(Session, monkeypatch):
    conn = _ConnFalsa()
    monkeypatch.setattr(sc, "_conn", lambda: conn)
    sc.consultar(MODELO, ["cant_SR"], ["Fecha:mes"], filtros={"Operador": ["Ana", "Beto"], "Source": []},
                 periodo={"campo": "Fecha", "relativo": "mes_actual"})
    sql, params = conn.ejecutado[-1]
    assert "DATE_FORMAT(`fecha`, '%%Y-%%m') AS `Fecha:mes`" in sql
    assert "`operador` IN (%s, %s)" in sql and "source" not in sql          # lista vacía = sin filtro
    assert "GROUP BY" in sql and "ORDER BY\n  DATE_FORMAT" in sql
    assert params[:2] == ["Ana", "Beto"] and params[2] == date.today().replace(day=1).isoformat()

    sc.consultar(MODELO, [], ["Operador"])
    assert conn.ejecutado[-1][0].startswith("SELECT DISTINCT")
    with pytest.raises(sc.ConsultaError, match="no es una dimensión de fecha"):
        sc.consultar(MODELO, ["cant_SR"], ["Operador:mes"])
    with pytest.raises(sc.ConsultaError, match="Granularidad inválida"):
        sc.consultar(MODELO, ["cant_SR"], ["Fecha:hora"])


# ── recetas ──────────────────────────────────────────────────────────────────

def test_validar_receta(Session):
    db = Session()
    r = vivos.validar_receta(db, RECETA)
    assert r["consultas"]["mensual"]["modelo"] == MODELO and r["filtros"][1]["default"] == []

    def error(**cambios):
        x = json.loads(json.dumps(RECETA))
        for ruta, valor in cambios.items():
            obj = x
            *pasos, ultimo = ruta.split(".")
            for p in pasos:
                obj = obj[int(p)] if p.isdigit() else obj[p]
            obj[ultimo] = valor
        with pytest.raises(TableroError) as e:
            vivos.validar_receta(db, x)
        return str(e.value)

    assert "métricas que no existen" in error(**{"consultas.ranking.metricas": ["Ventas"]})
    assert "no es un filtro de dimensión" in error(**{"consultas.mensual.filtros": {"Operador": "$periodo"}})
    assert "no es un filtro de período" in error(**{"consultas.ranking.periodo": "$operador"})
    assert "granularidad" in error(**{"consultas.ranking.dimensiones": ["Operador:mes"]})
    assert "campo_periodo" in error(**{"consultas.ranking.periodo": "ultimos_3_meses"})
    assert "tipo inválido" in error(**{"filtros.1.tipo": "texto"})
    assert "no existe" in error(modelo="otro")
    assert "inválido" in error(**{"filtros.0.default": "el_otro_martes"})
    db.close()


def test_valores_efectivos():
    r = {"filtros": [{"id": "periodo", "tipo": "periodo", "etiqueta": "P", "default": "ultimos_12_meses"},
                     {"id": "op", "tipo": "dimension", "etiqueta": "O", "multiple": True, "default": []},
                     {"id": "uno", "tipo": "dimension", "etiqueta": "U", "multiple": False, "default": None}]}
    assert vivos.valores_efectivos(r, {"periodo": "pasado_mañana", "op": "Ana", "uno": ["X", "Y"], "otro": 1}) == {
        "periodo": "ultimos_12_meses", "op": ["Ana"], "uno": "X"}                # inválidos → default
    assert vivos.valores_efectivos(r, {"periodo": {"desde": "2026-01", "hasta": "2026-03"}})["periodo"] == \
        {"desde": "2026-01", "hasta": "2026-03"}


def test_ejecutar_sustituye_filtros_y_cachea(Session):
    receta = vivos.validar_receta(Session(), RECETA)
    llamadas = []

    def fake(**kw):
        llamadas.append(kw)
        return {"columnas": kw["dimensiones"] + kw["metricas"], "filas": [["x", 1]]}

    vals = vivos.valores_efectivos(receta, {"operador": ["Ana"]})
    r = vivos.ejecutar(receta, vals, clave_cache="t:1", consultar_fn=fake)
    mensual = next(c for c in llamadas if c["dimensiones"] == ["Fecha:mes"])
    assert mensual["filtros"] == {"Operador": ["Ana"], "Source": "SR"}
    assert mensual["periodo"] == {"campo": "Fecha", "relativo": "ultimos_12_meses"}
    assert r["datos"]["ranking"]["filas"] == [["x", 1]] and not r["desde_cache"]
    assert vivos.ejecutar(receta, vals, clave_cache="t:1", consultar_fn=fake)["desde_cache"]
    assert len(llamadas) == 2                                                   # la 2.ª vino de la caché

    sin_op = vivos.ejecutar(receta, vivos.valores_efectivos(receta, {}), consultar_fn=fake)
    assert "Operador" not in llamadas[-2]["filtros"]                            # sin selección = sin filtro

    def falla(**kw):
        raise sc.ConsultaError("columna rota")
    r = vivos.ejecutar(receta, vals, clave_cache="t:2", consultar_fn=falla)
    assert r["datos"]["mensual"]["error"] == "columna rota"
    assert not vivos.ejecutar(receta, vals, clave_cache="t:2", consultar_fn=falla)["desde_cache"]  # errores no se cachean


def test_inyectar_sdk():
    html = vivos.inyectar_sdk("<html><head><title>x</title></head><body></body></html>", {"datos": {"a": 1}})
    assert html.index("window.__ORQ_DATOS__") < html.index("window.Tablero") < html.index("<title>")
    assert "</script>" in html and '"</' not in html.split("window.__ORQ_DATOS__")[1].split(";")[0]


# ── app ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def cliente(Session, monkeypatch):
    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()
    api.app.dependency_overrides[get_catalog_db] = override
    monkeypatch.setattr(api, "_consultar_dw", lambda **kw: {
        "columnas": kw["dimensiones"] + kw["metricas"],
        "filas": [["Ana"], ["Beto"]] if not kw["metricas"] else [["2026-09", 7]]})
    yield lambda *roles, usuario="u": iniciar_sesion(TestClient(api.app), Session, monkeypatch, roles=roles,
                                                       usuario=usuario, nombre=usuario.title())
    api.app.dependency_overrides.clear()


def _publicar_vivo(Session, autor="Ana"):
    from src.catalog import tableros_service as svc
    db = Session()
    t = svc.crear(db, "NOC vivo", HTML_VIVO, autor=autor, receta=vivos.validar_receta(db, RECETA))
    db.commit(); slug = t.slug; db.close()
    return slug


def test_visor_pide_datos_opciones_y_foto(Session, cliente):
    slug = _publicar_vivo(Session)
    lector = cliente("lector", usuario="lucia")
    cont = lector.get(f"/tableros/{slug}/contenido").text
    assert "window.Tablero" in cont and "window.__ORQ_DATOS__ =" not in cont

    r = lector.post(f"/api/tableros/{slug}/datos", json={"valores": {"operador": ["Ana"], "inventado": 1}}).json()
    assert r["ok"] and r["valores"] == {"periodo": "ultimos_12_meses", "operador": ["Ana"]}
    m = r["mensaje"]
    assert m["tipo"] == "orquestador:datos" and m["datos"]["mensual"]["filas"] == [["2026-09", 7]]
    assert m["filtros"]["operador"]["texto"] == "Ana" and " al " in m["filtros"]["periodo"]["texto"]

    assert lector.get(f"/api/tableros/{slug}/opciones/operador").json()["valores"] == ["Ana", "Beto"]
    assert not lector.get(f"/api/tableros/{slug}/opciones/periodo").json()["ok"]

    foto = lector.get(f"/tableros/{slug}/foto", params={"valores": json.dumps({"operador": ["Beto"]})})
    assert "attachment" in foto.headers["content-disposition"] and "sandbox" in foto.headers["content-security-policy"]
    assert '"valor": ["Beto"]' in foto.text and "window.__ORQ_DATOS__" in foto.text


def test_version_nueva_hereda_la_receta(Session, cliente):
    slug = _publicar_vivo(Session, autor="Ana")
    ana = cliente("analista", usuario="ana")
    subir = lambda html: ana.post(f"/api/tableros/{slug}/versiones",
                                  files={"file": ("v.html", io.BytesIO(html.encode()), "text/html")}).json()
    t = subir(HTML_VIVO.replace("</body>", "<p>retoque</p></body>"))["tablero"]
    assert t["tipo"] == "vivo" and t["versiones"][0]["receta"]["consultas"]
    t = subir("<html><body>foto</body></html>")["tablero"]
    assert t["tipo"] == "foto" and t["versiones"][0]["receta"] is None


# ── MCP ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def mcp(Session, monkeypatch):
    monkeypatch.setattr(mcp_server, "SessionCatalog", Session)
    monkeypatch.setattr(mcp_server, "_consultar", lambda **kw: {
        "columnas": kw["dimensiones"] + kw["metricas"], "filas": [["2026-09", 7]]})
    marca = mcp_server._usuario_actual.set(mcp_server._usuarios.SesionUsuario(
        1, "ana", "Ana", ("analista",), permisos_de(["analista"])))
    yield mcp_server
    mcp_server._usuario_actual.reset(marca)


def test_mcp_publica_tablero_vivo(mcp, monkeypatch):
    r = mcp.publicar_dashboard("NOC vivo", HTML_VIVO, receta=RECETA)
    assert "vivo: se actualiza al abrirlo" in r and "Slug: noc-vivo" in r
    assert '"consultas"' in mcp.obtener_dashboard("noc-vivo")

    assert "no lo usa" in mcp.publicar_dashboard("Otro", "<html>sin sdk</html>", receta=RECETA)
    assert "Error" in mcp.publicar_dashboard("Otro", HTML_VIVO, receta={**RECETA, "consultas": {}})

    def falla(**kw):
        raise sc.ConsultaError("Unknown column 'x'")
    monkeypatch.setattr(mcp_server, "_consultar", falla)
    r = mcp.publicar_dashboard("Roto", HTML_VIVO, receta=RECETA)
    assert "no publiqué" in r and "Unknown column" in r
    assert "Roto" not in mcp.listar_dashboards()


def test_mcp_probar_receta(mcp):
    r = mcp.probar_receta(RECETA, {"periodo": "ultimos_3_meses"})
    assert '"periodo": "ultimos_3_meses"' in r and "### mensual: 1 filas" in r and "2026-09 | 7" in r
    assert "Error en la receta" in mcp.probar_receta({"consultas": {}})


# ── cómo se armó ─────────────────────────────────────────────────────────────

def test_mcp_guarda_pedido_y_decisiones(mcp):
    r = mcp.publicar_dashboard("NOC vivo", HTML_VIVO, receta=RECETA,
                               pedido="Tablero del NOC por operador, filtrable por período",
                               decisiones="Visitas técnicas = work orders.\nSe excluyen casos sin operador.")
    assert "Aviso" not in r
    r = mcp.publicar_dashboard("", HTML_VIVO, actualizar="noc-vivo", nota="sumé WhatsApp")
    assert "no guardaste pedido ni decisiones" in r
    o = mcp.obtener_dashboard("noc-vivo")
    assert "Cómo se armó" in o and "Pedido: Tablero del NOC por operador" in o
    assert "Decisiones: Visitas técnicas = work orders." in o and "v2 (Ana" in o


def test_app_guarda_pedido_y_decisiones(Session, cliente):
    ana = cliente("analista", usuario="ana")
    t = ana.post("/api/tableros", data={"titulo": "Ventas", "pedido": "ventas por mes", "decisiones": "sin mudanzas"},
                 files={"file": ("v.html", io.BytesIO(b"<html>v</html>"), "text/html")}).json()["tablero"]
    v = t["versiones"][0]
    assert (v["pedido"], v["decisiones"]) == ("ventas por mes", "sin mudanzas")
    assert "Cómo se armó" in ana.get(f"/tableros/{t['slug']}").text
