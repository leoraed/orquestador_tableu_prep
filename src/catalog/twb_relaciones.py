"""
Fuentes de Tableau con modelo de relaciones (object-graph): varias tablas lógicas,
cada una una tabla física o una consulta SQL personalizada, unidas por relaciones.

Tableau combina las relaciones según lo que usa cada hoja, sin duplicar filas. Un
modelo semántico es un FROM fijo con JOINs, así que se arma un modelo por tabla
de hechos, con las tablas de dimensión que cuelgan de ella:

- Hechos: por defecto, la tabla con más relaciones y las que tienen métricas
  propias (que usan solo columnas de esa tabla). Se puede elegir al importar.
- Dimensiones de cada hecho: las unidas directamente y, para los hechos que se
  relacionan con la tabla central, también las de la central, reescribiendo la
  condición con la clave equivalente del hecho (central.cliente_id = hecho.cliente_id
  y central.cliente_id = dim.id  →  hecho.cliente_id = dim.id).
- Campos que mezclan dos tablas de hechos no se importan: se arman en el tablero
  con una consulta por modelo.

Este módulo no conoce la clase TwbDatasource: devuelve dicts con sus campos.
"""
import os
import re
from collections import Counter, deque

from src.catalog.tableau_to_sql import NoTraducible, _split_ref, make_resolver, translate_formula

DB_DW = os.getenv("CATALOG_MYSQL_DB", "orbith_bi")
_REF_RE = re.compile(r"\[(?:[^\]]|\]\])*\](?:\.\[(?:[^\]]|\]\])*\])?")
_INTERNO = "__tableau_internal_object_id__"
_INTERNO_SQL_RE = re.compile(r"`__tableau_internal_object_id__`\.`[^`]+`")
_AGREGACIONES = {"sum": "SUM({})", "count": "COUNT({})", "countd": "COUNT(DISTINCT {})", "avg": "AVG({})",
                 "min": "MIN({})", "max": "MAX({})"}
_FECHAS = {"date", "datetime"}
_IDENTIFICADOR_RE = re.compile(r"`[^`]*`(\.`[^`]*`)?")
_STRING_SQL_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'")


# ── SQL personalizado ────────────────────────────────────────────────────────

_PARAM_SQL_RE = re.compile(r"<\[Parameters\]\.\[[^\]]+\]>")


def desescapar_sql(sql: str) -> str:
    """Tableau guarda < y > duplicados en el SQL personalizado (los parámetros van como <[Parameters].[x]>)."""
    out, i = [], 0
    while i < len(sql):
        m = _PARAM_SQL_RE.match(sql, i)
        if m:
            out.append(m.group())
            i = m.end()
        elif sql[i:i + 2] in ("<<", ">>"):
            out.append(sql[i])
            i += 2
        else:
            out.append(sql[i])
            i += 1
    return "".join(out).strip()


def parametros_sql(sql: str) -> list[str]:
    return sorted(set(_PARAM_SQL_RE.findall(sql)))


_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"|--[^\n]*|#[^\n]*|/\*.*?\*/", re.S)
_TABLA_RE = re.compile(r"\b(FROM|JOIN)(\s+)(`?)([A-Za-z_][\w$]*)\3(?![\w$`.])", re.I)
_FUNCIONES_CON_FROM = {"trim", "extract", "substring", "substr", "position", "convert"}
_NO_TABLAS = {"dual", "lateral"}


def calificar_tablas(sql: str, base: str) -> tuple[str, list[str]]:
    """
    Agrega la base a las tablas sin calificar (FROM x / JOIN x → `base`.`x`): el SQL
    se escribió para la conexión de Tableau, que puede apuntar a otra base que la del DW.
    No toca literales, comentarios, CTEs ni el FROM de TRIM/EXTRACT/SUBSTRING.
    """
    enmascarado = _LITERAL_RE.sub(lambda m: " " * len(m.group()), sql)
    ctes = {n.lower() for n in re.findall(r"(?:\bWITH\s+(?:RECURSIVE\s+)?|,\s*)(\w+)\s+AS\s*\(", enmascarado, re.I)}
    partes, ultimo, calificadas = [], 0, []
    for m in _TABLA_RE.finditer(enmascarado):
        nombre = m.group(4)
        if nombre.lower() in _NO_TABLAS | ctes or _dentro_de_funcion(enmascarado, m.start()):
            continue
        partes.append(sql[ultimo:m.start()] + f"{m.group(1)}{m.group(2)}`{base}`.`{nombre}`")
        ultimo = m.end()
        if nombre not in calificadas:
            calificadas.append(nombre)
    return "".join(partes) + sql[ultimo:], calificadas


def _dentro_de_funcion(texto: str, pos: int) -> bool:
    nivel = 0
    for j in range(pos - 1, -1, -1):
        c = texto[j]
        if c == ")":
            nivel += 1
        elif c == "(":
            if nivel == 0:
                fn = re.search(r"([A-Za-z_]\w*)\s*$", texto[:j])
                return bool(fn) and fn.group(1).lower() in _FUNCIONES_CON_FROM
            nivel -= 1
    return False


# ── lectura del object-graph ─────────────────────────────────────────────────

def es_modelo_de_relaciones(ds) -> bool:
    og = ds.find("object-graph")
    if og is None:
        return False
    objetos = og.findall(".//object")
    return len(objetos) > 1 or any(r.get("type") == "text" for o in objetos for r in o.iter("relation"))


def _relacion_principal(obj):
    for p in obj.findall("properties"):
        if p.get("context", "") == "":
            return p.find("relation")
    return None


def _pares(expr) -> list[tuple[str, str]]:
    """Expresión de una relación → [(fórmula lado 1, fórmula lado 2)] (AND de igualdades)."""
    op, hijos = expr.get("op", ""), list(expr)
    if op == "AND":
        return [p for h in hijos for p in _pares(h)]
    if op == "=" and len(hijos) == 2:
        return [(hijos[0].get("op", "").strip(), hijos[1].get("op", "").strip())]
    return [(None, None)]          # otra comparación: no se puede usar como join


def importar(ds, defs: dict, dependencias: dict, usados: set, ds_names: set, hechos_elegidos=None) -> list[dict]:
    ds_name = ds.get("name", "")
    caption = ds.get("caption", "") or ds_name
    conn = ds.find("connection")
    og = ds.find("object-graph")

    bases = {}
    for nc in conn.iter("named-connection"):
        c = nc.find("connection")
        if c is not None:
            bases[nc.get("name")] = c.get("dbname") or ""

    # Tablas lógicas
    objetos, rel_a_alias, id_a_alias, avisos_generales = {}, {}, {}, []
    for o in og.iter("object"):
        alias = o.get("caption") or o.get("id")
        while alias in objetos:
            alias += "_"
        rel = _relacion_principal(o)
        id_a_alias[o.get("id")] = alias
        if rel is None or rel.get("type") not in ("text", "table"):
            avisos_generales.append(f"La tabla «{alias}» es una combinación de tablas ({rel.get('type') if rel is not None else '?'}) "
                                    "que no se puede importar: armala como consulta SQL en el editor.")
            continue
        base = bases.get(rel.get("connection"), "")
        info = {"alias": alias, "avisos": [], "calificadas": []}
        if rel.get("type") == "text":
            sql = desescapar_sql(rel.text or "")
            params = parametros_sql(sql)
            if params:
                info["avisos"].append(f"La consulta de «{alias}» usa parámetros de Tableau ({', '.join(params)}): "
                                      "reemplazalos por un valor en el editor.")
            if base and base != DB_DW:
                sql, calificadas = calificar_tablas(sql, base)
                info["calificadas"] = [(base, t) for t in calificadas]
            info["sql"] = sql
        else:
            partes = _split_ref(rel.get("table", ""))
            tabla = partes[-1] if partes else ""
            esquema = partes[0] if len(partes) == 2 else base
            if esquema and esquema != DB_DW:
                info["sql"] = f"SELECT * FROM `{esquema}`.`{tabla}`"
            else:
                info["tabla"] = tabla
        objetos[alias] = info
        rel_a_alias[rel.get("name")] = alias

    # Columnas físicas: clave → (alias, columna, tipo, agregación)
    columnas = {}
    for m in conn.iter("metadata-record"):
        if m.get("class") != "column":
            continue
        local, padre = m.findtext("local-name"), (m.findtext("parent-name") or "").strip("[]")
        if local and padre in rel_a_alias:
            columnas[local] = (rel_a_alias[padre], m.findtext("remote-name") or local.strip("[]"),
                               (m.findtext("local-type") or "string").lower(), (m.findtext("aggregation") or "").lower())
    cols = conn.find("cols")
    for m in (cols.findall("map") if cols is not None else []):
        key, val = m.get("key", ""), m.get("value", "")
        if key not in columnas and "].[" in val:
            rel, remota = val.split("].[", 1)
            if rel.strip("[]") in rel_a_alias:
                columnas[key] = (rel_a_alias[rel.strip("[]")], remota.strip("[]"), "string", "")

    # Campos: declarados en la fuente + columnas físicas que usan las hojas sin declararlas
    campos = dict(defs)
    for key, dep in dependencias.items():
        if key not in campos and key in columnas:
            campos[key] = {"caption": dep.get("caption") or key.strip("[]"), "role": dep.get("role", ""),
                           "datatype": dep.get("datatype") or columnas[key][2], "formula": "", "fmt": "", "agg": ""}
    for key, info in campos.items():
        # «cliente_id (Custom SQL Query)» → «cliente_id (Link)»
        m = re.match(r"^(.*) \(([^()]+)\)$", info["caption"])
        if m and m.group(2) in rel_a_alias:
            info["caption"] = f"{m.group(1)} ({rel_a_alias[m.group(2)]})"

    resolver = make_resolver(campos, {k: (v[0], v[1]) for k, v in columnas.items()}, "", ds_names, ds_name)

    def traducir(formula: str) -> str:
        sql = _INTERNO_SQL_RE.sub("1", translate_formula(formula, resolver))
        # Toda columna va con su tabla: un `nombre` suelto es un conjunto o grupo de Tableau
        suelto = next((m for m in _IDENTIFICADOR_RE.finditer(_STRING_SQL_RE.sub("''", sql)) if not m.group(1)), None)
        if suelto:
            raise NoTraducible(f"{suelto.group()} no es una columna (¿conjunto o grupo de Tableau?)")
        return sql

    def objetos_de(key: str, visitados=None) -> set:
        visitados = visitados if visitados is not None else set()
        if key in visitados:
            return set()
        visitados.add(key)
        partes = _split_ref(key)
        if partes and partes[0] == _INTERNO:
            return {id_a_alias.get(partes[1])} - {None}
        if len(partes) == 2:
            if partes[0] == "Parameters" or partes[0] != ds_name:
                return set()
            key = f"[{partes[1]}]"
        if key in columnas:
            return {columnas[key][0]}
        formula = (campos.get(key) or {}).get("formula") or ""
        return set().union(*[objetos_de(r, visitados) for r in _REF_RE.findall(formula)]) if formula else set()

    def objetos_formula(formula: str) -> set:
        return set().union(*[objetos_de(r) for r in _REF_RE.findall(formula)]) if formula else set()

    # Relaciones
    aristas = []
    for r in og.iter("relationship"):
        extremos = [r.find("first-end-point"), r.find("second-end-point")]
        a, b = [id_a_alias.get(e.get("object-id")) if e is not None else None for e in extremos]
        if a not in objetos or b not in objetos or r.find("expression") is None:
            continue
        pares = []
        for f1, f2 in _pares(r.find("expression")):
            if f1 is None:
                pares.append(None)
                continue
            if objetos_formula(f1) <= {b} and objetos_formula(f2) <= {a}:
                f1, f2 = f2, f1
            try:
                pares.append({"fa": f1, "fb": f2, "a": traducir(f1), "b": traducir(f2)})
            except NoTraducible:
                pares.append({"fa": f1, "fb": f2, "a": None, "b": None})
        aristas.append({"a": a, "b": b, "pares": pares})

    def on_de(arista) -> str | None:
        """Condición ON de la relación; None si alguna parte no se puede traducir."""
        if any(p is None or p["a"] is None for p in arista["pares"]):
            return None
        return " AND ".join(f"{p['a']} = {p['b']}" for p in arista["pares"])

    grado = Counter([x["a"] for x in aristas] + [x["b"] for x in aristas])
    central = max(objetos, key=lambda o: (grado[o], -list(objetos).index(o)))

    # Campos usados: tablas que tocan y expresión SQL
    usados_info = []
    for key in sorted(usados or campos):
        info = campos.get(key)
        if not info or not info.get("role"):
            continue
        objs = objetos_de(key)
        es_conteo = _split_ref(key)[:1] == [_INTERNO]
        usados_info.append({"key": key, "info": info, "objs": objs, "conteo": es_conteo})

    # Hechos sugeridos: la central + las que tienen métricas propias
    propias = Counter(next(iter(u["objs"])) for u in usados_info
                      if u["info"]["role"] == "measure" and len(u["objs"]) == 1 and not u["conteo"])
    sugeridos = {central} | {o for o, n in propias.items() if n > 0}
    hechos = set(hechos_elegidos) & set(objetos) if hechos_elegidos else sugeridos
    if not hechos:
        hechos = sugeridos
    tablas = [{"nombre": o, "hecho": o in hechos, "sugerido": o in sugeridos,
               "relaciones": grado[o], "metricas_propias": propias.get(o, 0)} for o in objetos]

    def unir(hecho: str) -> tuple[list[dict], set, list[str]]:
        joins, incluidas, avisos = [], {hecho}, []
        cola = deque()

        def agregar(alias: str, on: str):
            o = objetos[alias]
            j = {"table": alias, "alias": alias, "on": on, "type": "LEFT"}
            if "sql" in o:
                j["sql"] = o["sql"]
            else:
                j["table"] = o["tabla"]
            joins.append(j)
            incluidas.add(alias)
            cola.append(alias)

        def expandir():
            while cola:
                actual = cola.popleft()
                for x in aristas:
                    if actual not in (x["a"], x["b"]):
                        continue
                    otro = x["b"] if x["a"] == actual else x["a"]
                    if otro in incluidas or otro in hechos:
                        continue
                    on = on_de(x)
                    if on is None:
                        avisos.append(f"La relación entre «{actual}» y «{otro}» usa algo que no se puede traducir "
                                      f"a SQL: «{otro}» no se unió; definí el join en el editor.")
                        continue
                    agregar(otro, on)

        cola.append(hecho)
        expandir()
        # Dimensiones de la tabla central, con la clave equivalente de este hecho
        if hecho != central:
            arista_c = next((x for x in aristas if {x["a"], x["b"]} == {hecho, central}), None)
            equivalentes = {}
            for p in (arista_c["pares"] if arista_c else []):
                if p and p["a"] is not None:
                    lado_c, lado_h = (p["a"], p["b"]) if arista_c["a"] == central else (p["b"], p["a"])
                    equivalentes[lado_c] = lado_h
            for x in aristas:
                if central not in (x["a"], x["b"]):
                    continue
                dim = x["b"] if x["a"] == central else x["a"]
                if dim in incluidas or dim in hechos or not equivalentes:
                    continue
                pares = []
                for p in x["pares"]:
                    if p is None or p["a"] is None:
                        pares = None
                        break
                    lado_c, lado_d = (p["a"], p["b"]) if x["a"] == central else (p["b"], p["a"])
                    if lado_c not in equivalentes:
                        pares = None
                        break
                    pares.append(f"{equivalentes[lado_c]} = {lado_d}")
                if pares:
                    agregar(dim, " AND ".join(pares))
            expandir()
        return joins, incluidas, avisos

    def fecha_comun(hecho: str):
        """Columna de fecha del hecho con la que se relaciona por fecha con otra tabla (la central, si hay)."""
        candidatas = [x for x in aristas if {x["a"], x["b"]} == {hecho, central}] if hecho != central else             [x for x in aristas if hecho in (x["a"], x["b"])]
        for x in candidatas:
            for p in x["pares"]:
                if not p:
                    continue
                f_hecho = p["fa"] if x["a"] == hecho else p["fb"]
                refs = [r for r in _REF_RE.findall(f_hecho or "") if r in columnas]
                if len(refs) == 1 and columnas[refs[0]][0] == hecho and columnas[refs[0]][2] in _FECHAS:
                    return f"`{hecho}`.`{columnas[refs[0]][1]}`", columnas[refs[0]][2]
        return None

    # Traducción de cada campo (una vez; los alias son los mismos en todos los modelos)
    for u in usados_info:
        info, formula = u["info"], u["info"]["formula"]
        u["tableau"] = _formula_legible(formula, campos) if formula else None
        try:
            if u["conteo"]:
                u["sql"] = "COUNT(*)"
            elif formula:
                u["sql"] = traducir(formula)
            else:
                col = resolver(u["key"])
                agg = (info.get("agg") or (columnas.get(u["key"]) or ("", "", "", ""))[3] or "sum").lower()
                u["sql"] = _AGREGACIONES.get(agg, "SUM({})").format(col) if info["role"] == "measure" else col
            u["error"] = None
        except NoTraducible as exc:
            u["sql"], u["error"] = u["tableau"], str(exc)

    cruces = [u for u in usados_info if len(u["objs"] & hechos) >= 2]
    # Fecha común a todos los modelos del workbook: «Fecha», salvo que ya sea otro campo
    nombre_fecha = "Fecha del hecho" if any(u["info"]["caption"] == "Fecha" for u in usados_info) else "Fecha"
    principal = central if central in hechos else next(o for o in objetos if o in hechos)
    modelos = []
    for hecho in [o for o in objetos if o in hechos]:
        joins, incluidas, avisos = unir(hecho)
        avisos = list(avisos_generales) + avisos
        calificadas = {}
        for alias in [hecho] + [j["alias"] for j in joins]:
            avisos += objetos[alias]["avisos"]
            for base, tabla in objetos[alias]["calificadas"]:
                calificadas.setdefault(base, {}).setdefault(tabla, None)
        for base, tablas_base in calificadas.items():
            avisos.append(f"Las consultas SQL usan tablas de «{base}» sin indicar la base: se agregó «{base}.» a "
                          f"{', '.join(tablas_base)} (el DW se consulta desde «{DB_DW}»).")
        dims, mets, pendientes, vistos, fuera = [], [], [], set(), []
        for u in usados_info:
            objs, en_hechos = u["objs"], u["objs"] & hechos
            if u["conteo"] and objs != {hecho}:
                continue
            if en_hechos and en_hechos != {hecho}:
                continue
            if not objs and hecho != principal:
                continue
            if not objs <= incluidas:
                if hecho in objs:
                    fuera.append(u["info"]["caption"])
                continue
            nombre = u["info"]["caption"]
            if nombre in vistos:
                continue
            vistos.add(nombre)
            if u["error"]:
                pendientes.append({"name": nombre, "kind": "dimension" if u["info"]["role"] == "dimension" else "metric",
                                   "reason": u["error"]})
            campo = {"name": nombre, "expression": u["sql"], "description": "", "tableau_formula": u["tableau"]}
            if u["info"]["role"] == "dimension":
                dims.append({**campo, "data_type": u["info"]["datatype"]})
            else:
                mets.append({**campo, "format": u["info"].get("fmt", "")})
        fecha = fecha_comun(hecho)
        if fecha and nombre_fecha not in vistos:
            dims.append({"name": nombre_fecha, "expression": fecha[0], "data_type": fecha[1], "tableau_formula": None,
                         "description": f"Fecha de «{hecho}» con la que se relaciona con las otras tablas: "
                                        "usala para cruzar este modelo con los otros del mismo workbook."})
        propios_cruce = [u["info"]["caption"] for u in cruces if hecho in u["objs"]]
        if propios_cruce:
            avisos.append(f"{len(propios_cruce)} campos combinan «{hecho}» con otra tabla de hechos y no se importaron "
                          f"(ej. {', '.join(sorted(propios_cruce)[:6])}): armalos en el tablero con una consulta por modelo.")
        if fuera:
            avisos.append(f"{len(fuera)} campos usan tablas que no quedaron unidas a «{hecho}» y no se importaron "
                          f"(ej. {', '.join(sorted(fuera)[:6])}).")
        o = objetos[hecho]
        modelos.append({
            "model_name": f"{_nombre(caption)}_{_nombre(hecho)}",
            "caption": f"{caption} · {hecho}",
            "description": f"Importado desde Tableau Workbook (fuente: {caption}, tabla de hechos: {hecho})",
            "base_table": hecho if "sql" in o else o["tabla"],
            "base_sql": o.get("sql"),
            "joins": joins,
            "dimensions": sorted(dims, key=lambda x: x["name"]),
            "metrics": sorted(mets, key=lambda x: x["name"]),
            "pendientes": sorted(pendientes, key=lambda x: x["name"]),
            "avisos": list(dict.fromkeys(avisos)),
            "grupo": caption,
            "tablas": tablas,
        })
    return modelos


def _nombre(texto: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", texto.lower())
    return s.strip("_") or "modelo"


def _formula_legible(formula: str, defs: dict) -> str:
    def repl(m):
        info = defs.get(m.group())
        if info and info["caption"] != m.group().strip("[]"):
            return "[" + info["caption"].replace("]", "]]") + "]"
        return m.group()
    return _REF_RE.sub(repl, formula)
