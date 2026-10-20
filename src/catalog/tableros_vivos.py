"""
Tableros vivos: una plantilla HTML + una receta (consultas y filtros) que el
orquestador ejecuta cada vez que alguien abre el tablero.

Receta:
    {
      "modelo": "stagedata_noc_link_orbith_bi",          # modelo por defecto de las consultas
      "filtros": [
        {"id": "periodo", "tipo": "periodo", "etiqueta": "Período", "campo": "Fecha",
         "default": "ultimos_12_meses"},
        {"id": "operador", "tipo": "dimension", "etiqueta": "Operador",
         "dimension": "Nombre Completo", "multiple": true, "default": []},
        {"id": "grano", "tipo": "granularidad", "etiqueta": "Ver por",
         "opciones": ["mes", "semana", "dia"], "default": "mes"}
      ],
      "consultas": {
        "evolucion": {"metricas": ["cant_SR"], "dimensiones": ["Fecha:$grano"],
                      "filtros": {"Nombre Completo": "$operador"}, "periodo": "$periodo"},
        "ranking": {"metricas": ["cant_SR"], "dimensiones": ["Nombre Completo"], "periodo": "$periodo"}
      }
    }

En una consulta, "$id" toma el valor actual del filtro declarado con ese id.
Un filtro de granularidad se usa en una dimensión de fecha ("Fecha:$grano"): la
columna resultante se llama con el grano elegido ("Fecha:semana").

Como el tablero se muestra aislado (sin acceso a la API), el visor del
orquestador ejecuta la receta y le pasa los datos por postMessage; la plantilla
los recibe con Tablero.alRecibirDatos(fn) (ver SDK_JS).
"""
import copy
import json
import re
import time
from datetime import date

from sqlalchemy.orm import Session

from src.catalog.models_semantic import SemanticModel
from src.catalog.sql_compiler import (
    GRANOS, ConsultaError, PERIODOS_RELATIVOS, consultar, resolver_periodo_relativo,
)
from src.catalog.tableros_service import TableroError

MAX_CONSULTAS = 20
MAX_VALORES_FILTRO = 100
MAX_OPCIONES = 500
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_MES_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
NOMBRES_GRANO = {"dia": "Día", "semana": "Semana", "mes": "Mes", "trimestre": "Trimestre", "anio": "Año"}


# ── validación ───────────────────────────────────────────────────────────────

def _campos_modelo(db: Session, nombre: str) -> tuple[dict, set]:
    m = db.query(SemanticModel).filter(SemanticModel.name == nombre).first()
    if not m or not m.entities:
        raise TableroError(f"El modelo «{nombre}» no existe (o no tiene entidades).")
    e = m.entities[0]
    return {d.name: (d.data_type or "string").lower() for d in e.dimensions}, {x.name for x in e.metrics}


def _es_ref(valor) -> bool:
    return isinstance(valor, str) and valor.startswith("$")


def _validar_periodo_literal(valor, donde: str) -> None:
    if isinstance(valor, str):
        resolver_periodo_relativo(valor)          # lanza ConsultaError si no es válido
    elif isinstance(valor, dict):
        for k in ("desde", "hasta"):
            if valor.get(k) and not _MES_RE.match(str(valor[k])):
                raise TableroError(f"{donde}: «{k}» tiene que ser AAAA-MM o AAAA-MM-DD.")
    else:
        raise TableroError(f"{donde}: el período tiene que ser un relativo (ej. «ultimos_12_meses») "
                           "o {\"desde\", \"hasta\"}.")


def validar_receta(db: Session, receta) -> dict:
    """Valida la estructura y los campos contra el modelo. Devuelve una copia normalizada."""
    try:
        return _validar(db, receta)
    except ConsultaError as e:
        raise TableroError(str(e))


def _validar(db: Session, receta) -> dict:
    if not isinstance(receta, dict):
        raise TableroError("La receta tiene que ser un objeto JSON.")
    r = copy.deepcopy(receta)
    modelo_def = r.get("modelo")
    consultas = r.get("consultas")
    if not isinstance(consultas, dict) or not consultas:
        raise TableroError("La receta necesita al menos una consulta en «consultas».")
    if len(consultas) > MAX_CONSULTAS:
        raise TableroError(f"Máximo {MAX_CONSULTAS} consultas por tablero.")
    cache_modelos: dict[str, tuple[dict, set]] = {}

    def campos(nombre):
        if nombre not in cache_modelos:
            cache_modelos[nombre] = _campos_modelo(db, nombre)
        return cache_modelos[nombre]

    # Filtros
    filtros = r.get("filtros") or []
    if not isinstance(filtros, list):
        raise TableroError("«filtros» tiene que ser una lista.")
    por_id = {}
    for f in filtros:
        fid = f.get("id") if isinstance(f, dict) else None
        if not fid or not _ID_RE.match(fid):
            raise TableroError(f"Filtro con id inválido: {fid!r} (minúsculas, números y _).")
        if fid in por_id:
            raise TableroError(f"Filtro repetido: {fid}.")
        if f.get("tipo") == "granularidad":
            f.setdefault("etiqueta", "Ver por")
            opciones = f.get("opciones") or ["mes", "semana", "dia"]
            if not isinstance(opciones, list) or any(o not in GRANOS for o in opciones):
                raise TableroError(f"Filtro «{fid}»: «opciones» tiene que ser una lista de {', '.join(GRANOS)}.")
            f["opciones"] = list(dict.fromkeys(opciones))
            f.setdefault("default", f["opciones"][0])
            if f["default"] not in f["opciones"]:
                raise TableroError(f"Filtro «{fid}»: el default «{f['default']}» no está entre las opciones.")
            por_id[fid] = f
            continue
        modelo = f.get("modelo") or modelo_def
        if not modelo:
            raise TableroError(f"Filtro «{fid}»: falta el modelo (en el filtro o en la receta).")
        dims, _ = campos(modelo)
        f.setdefault("etiqueta", fid.replace("_", " ").capitalize())
        if f.get("tipo") == "periodo":
            campo = f.get("campo")
            if dims.get(campo) not in ("date", "datetime"):
                raise TableroError(f"Filtro «{fid}»: «{campo}» no es una dimensión de fecha de «{modelo}».")
            f.setdefault("default", "ultimos_12_meses")
            _validar_periodo_literal(f["default"], f"Filtro «{fid}»")
        elif f.get("tipo") == "dimension":
            if f.get("dimension") not in dims:
                raise TableroError(f"Filtro «{fid}»: la dimensión «{f.get('dimension')}» no existe en «{modelo}».")
            f["multiple"] = bool(f.get("multiple", True))
            f.setdefault("default", [] if f["multiple"] else None)
        else:
            raise TableroError(f"Filtro «{fid}»: tipo inválido {f.get('tipo')!r} (periodo, dimension o granularidad).")
        por_id[fid] = f
    r["filtros"] = filtros

    # Consultas
    for nombre, c in consultas.items():
        donde = f"Consulta «{nombre}»"
        if not _ID_RE.match(nombre):
            raise TableroError(f"{donde}: el nombre tiene que ser minúsculas, números y _.")
        if not isinstance(c, dict):
            raise TableroError(f"{donde}: tiene que ser un objeto.")
        modelo = c.get("modelo") or modelo_def
        if not modelo:
            raise TableroError(f"{donde}: falta el modelo.")
        c["modelo"] = modelo
        dims, mets = campos(modelo)
        c["metricas"] = list(c.get("metricas") or [])
        c["dimensiones"] = list(c.get("dimensiones") or [])
        if not c["metricas"] and not c["dimensiones"]:
            raise TableroError(f"{donde}: necesita métricas o dimensiones.")
        faltan = [x for x in c["metricas"] if x not in mets]
        if faltan:
            raise TableroError(f"{donde}: métricas que no existen en «{modelo}»: {faltan}.")
        for d in c["dimensiones"]:
            base, _, grano = d.partition(":")
            if base not in dims:
                raise TableroError(f"{donde}: la dimensión «{base}» no existe en «{modelo}».")
            if _es_ref(grano):
                ref = por_id.get(grano[1:])
                if not ref or ref["tipo"] != "granularidad":
                    raise TableroError(f"{donde}: «{grano}» no es un filtro de granularidad declarado.")
                if dims[base] not in ("date", "datetime"):
                    raise TableroError(f"{donde}: «{d}» — la granularidad solo aplica a fechas.")
            elif grano and (grano not in GRANOS or dims[base] not in ("date", "datetime")):
                raise TableroError(f"{donde}: «{d}» — la granularidad solo aplica a fechas ({', '.join(GRANOS)}).")
        cf = c.get("filtros") or {}
        if not isinstance(cf, dict):
            raise TableroError(f"{donde}: «filtros» tiene que ser {{dimension: valor}}.")
        for dim, valor in cf.items():
            if dim not in dims:
                raise TableroError(f"{donde}: filtro sobre «{dim}», que no existe en «{modelo}».")
            if _es_ref(valor):
                ref = por_id.get(valor[1:])
                if not ref or ref["tipo"] != "dimension":
                    raise TableroError(f"{donde}: «{valor}» no es un filtro de dimensión declarado.")
        c["filtros"] = cf
        per = c.get("periodo")
        if per is not None:
            if _es_ref(per):
                ref = por_id.get(per[1:])
                if not ref or ref["tipo"] != "periodo":
                    raise TableroError(f"{donde}: «{per}» no es un filtro de período declarado.")
                campo = ref["campo"]
            else:
                _validar_periodo_literal(per, donde)
                campo = c.get("campo_periodo")
                if not campo:
                    raise TableroError(f"{donde}: con un período fijo indicá «campo_periodo» (dimensión de fecha).")
            if dims.get(campo) not in ("date", "datetime"):
                raise TableroError(f"{donde}: «{campo}» no es una dimensión de fecha de «{modelo}».")
        c["limite"] = max(1, min(int(c.get("limite") or 1000), 5000))
    r["consultas"] = consultas
    return r


# ── valores de los filtros ───────────────────────────────────────────────────

def valores_efectivos(receta: dict, pedidos: dict | None) -> dict:
    """Valores de filtro a usar: los pedidos (si son válidos para el filtro) o el default."""
    pedidos = pedidos or {}
    out = {}
    for f in receta.get("filtros") or []:
        v = pedidos.get(f["id"], f.get("default"))
        if f["tipo"] == "periodo":
            try:
                _validar_periodo_literal(v, f["etiqueta"])
            except (TableroError, ConsultaError):
                v = f.get("default")
        elif f["tipo"] == "granularidad":
            v = v if v in f["opciones"] else f["default"]
        else:
            if f["multiple"]:
                v = [str(x) for x in (v if isinstance(v, list) else ([] if v in (None, "") else [v]))]
                v = v[:MAX_VALORES_FILTRO]
            else:
                v = None if v in (None, "", []) else str(v[0] if isinstance(v, list) else v)
        out[f["id"]] = v
    return out


def _periodo_para(per, campo, valores) -> dict | None:
    if per is None:
        return None
    if _es_ref(per):
        per = valores.get(per[1:])
    if isinstance(per, str):
        return {"campo": campo, "relativo": per}
    return {"campo": campo, "desde": per.get("desde") or None, "hasta": per.get("hasta") or None}


def describir_periodo(valor) -> str:
    """Texto para mostrar: 'ultimos_12_meses' → '01/11/2025 al 19/10/2026'."""
    try:
        if isinstance(valor, str):
            d, h = resolver_periodo_relativo(valor)
        else:
            d, h = valor.get("desde") or "…", valor.get("hasta") or "…"
        fmt = lambda x: "/".join(reversed(x.split("-"))) if x != "…" else x
        return f"{fmt(d)} al {fmt(h)}"
    except Exception:
        return str(valor)


# ── ejecución ────────────────────────────────────────────────────────────────

def _resolver_grano(dimension: str, valores: dict) -> str:
    """'Fecha:$grano' → 'Fecha:semana' según el valor del filtro."""
    base, _, grano = dimension.partition(":")
    return f"{base}:{valores.get(grano[1:])}" if _es_ref(grano) else dimension


_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_SEGUNDOS = 10 * 60
_CACHE_MAX = 300


def ejecutar(receta: dict, valores: dict, clave_cache: str | None = None, refrescar: bool = False,
             consultar_fn=consultar) -> dict:
    """
    Ejecuta todas las consultas con los valores de filtro dados.
    Devuelve {"datos": {consulta: {columnas, filas, error}}, "consultado": iso, "desde_cache": bool}.
    """
    clave = f"{clave_cache}|{json.dumps(valores, sort_keys=True, ensure_ascii=False)}" if clave_cache else None
    ahora = time.time()
    if clave and not refrescar and clave in _CACHE and ahora - _CACHE[clave][0] < CACHE_SEGUNDOS:
        return {**_CACHE[clave][1], "desde_cache": True}

    por_id = {f["id"]: f for f in receta.get("filtros") or []}
    datos = {}
    for nombre, c in receta["consultas"].items():
        filtros = {}
        for dim, v in (c.get("filtros") or {}).items():
            v = valores.get(v[1:]) if _es_ref(v) else v
            if v is None or v == []:
                continue
            filtros[dim] = v
        per = c.get("periodo")
        campo = por_id[per[1:]]["campo"] if _es_ref(per) else c.get("campo_periodo")
        try:
            dims = [_resolver_grano(d, valores) for d in c["dimensiones"]]
            r = consultar_fn(modelo=c["modelo"], metricas=c["metricas"], dimensiones=dims,
                             filtros=filtros, periodo=_periodo_para(per, campo, valores), limite=c["limite"])
            datos[nombre] = {"columnas": r["columnas"], "filas": r["filas"], "error": None}
        except ConsultaError as e:
            datos[nombre] = {"columnas": [], "filas": [], "error": str(e)}
        except Exception as e:  # error del DW: se informa en esa consulta, el resto sigue
            datos[nombre] = {"columnas": [], "filas": [], "error": f"Error al consultar: {e}"}
    resultado = {"datos": datos, "consultado": time.strftime("%Y-%m-%dT%H:%M:%S"), "desde_cache": False}
    if clave and all(d["error"] is None for d in datos.values()):
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]))
        _CACHE[clave] = (ahora, resultado)
    return resultado


def opciones_filtro(receta: dict, filtro_id: str, consultar_fn=consultar) -> list:
    f = next((x for x in receta.get("filtros") or [] if x["id"] == filtro_id), None)
    if not f or f["tipo"] != "dimension":
        raise TableroError(f"El tablero no tiene un filtro de dimensión «{filtro_id}».")
    modelo = f.get("modelo") or receta.get("modelo")
    r = consultar_fn(modelo=modelo, metricas=[], dimensiones=[f["dimension"]], filtros={}, periodo=None,
                     limite=MAX_OPCIONES)
    return [fila[0] for fila in r["filas"] if fila[0] not in (None, "")]


def receta_heredada(tablero, html: str) -> dict | None:
    """Versión nueva sin receta: si el HTML sigue siendo de un tablero vivo, conserva la receta anterior."""
    anterior = tablero.ultima.receta if tablero.ultima else None
    return anterior if anterior and "Tablero.alRecibirDatos" in html else None


# ── SDK que se inyecta en el HTML ────────────────────────────────────────────

SDK_JS = r"""
<script>
/* Orquestador · SDK de tableros vivos (inyectado automáticamente) */
window.Tablero = (function () {
  var subs = [], ultimo = window.__ORQ_DATOS__ || null;
  function avisar(m) { subs.forEach(function (f) { try { f(m.datos, m.filtros, m); } catch (e) { console.error(e); } }); }
  window.addEventListener('message', function (e) {
    if (e.source !== window.parent) return;
    var m = e.data || {};
    if (m.tipo === 'orquestador:cargando') document.documentElement.classList.add('orq-cargando');
    if (m.tipo === 'orquestador:datos') {
      ultimo = m; document.documentElement.classList.remove('orq-cargando'); avisar(m);
    }
  });
  if (window.parent !== window) window.parent.postMessage({ tipo: 'orquestador:listo' }, '*');
  var estilo = document.createElement('style');
  estilo.textContent = 'html.orq-cargando body{opacity:.55;transition:opacity .15s}';
  document.head.appendChild(estilo);
  return {
    /* fn(datos, filtros, info): datos = {consulta: {columnas, filas, error}} */
    alRecibirDatos: function (fn) { subs.push(fn); if (ultimo) setTimeout(function () { fn(ultimo.datos, ultimo.filtros, ultimo); }); },
    /* Filas de una consulta como objetos {columna: valor} */
    filas: function (consulta) {
      if (!consulta) return [];
      return consulta.filas.map(function (f) { var o = {}; consulta.columnas.forEach(function (c, i) { o[c] = f[i]; }); return o; });
    },
  };
})();
</script>
"""


def inyectar_sdk(html: str, datos_iniciales: dict | None = None) -> str:
    """Agrega el SDK al <head> (y opcionalmente los datos, para descargar una foto que funcione sola)."""
    bloque = SDK_JS
    if datos_iniciales is not None:
        js = json.dumps(datos_iniciales, ensure_ascii=False, default=str).replace("</", "<\\/")
        bloque = f"<script>window.__ORQ_DATOS__ = {js};</script>\n" + SDK_JS
    m = re.search(r"<head[^>]*>", html, re.I)
    if m:
        return html[:m.end()] + bloque + html[m.end():]
    return bloque + html


def mensaje_datos(resultado: dict, valores: dict, receta: dict) -> dict:
    """Mensaje que recibe el tablero: datos + filtros con su descripción legible."""
    filtros = {}
    for f in receta.get("filtros") or []:
        v = valores.get(f["id"])
        if f["tipo"] == "periodo":
            texto = describir_periodo(v)
        elif f["tipo"] == "granularidad":
            texto = NOMBRES_GRANO.get(v, v)
        else:
            texto = (", ".join(v) if isinstance(v, list) else (v or "")) or "Todos"
        filtros[f["id"]] = {"valor": v, "etiqueta": f["etiqueta"], "texto": texto}
    return {"tipo": "orquestador:datos", "datos": resultado["datos"], "filtros": filtros,
            "consultado": resultado["consultado"], "hoy": date.today().isoformat()}
