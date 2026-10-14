"""
Relaciones entre flows del catálogo: qué flow escribe cada tabla y cuáles la leen.

Se cruzan los outputs (destination_table) con las fuentes (source_table) de
todos los flows. Particularidades de los datos parseados de los .tfl:
- Outputs a veces vienen entre corchetes: "[rawdata_mysql_orbith_contacto_historia]".
- Fuentes MySQL con consulta personalizada no tienen source_table; el nombre
  del nodo suele ser la tabla, a veces con sufijo de duplicado ("contacto 2").
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field

# Orden en que se muestran las capas (de la más cercana al consumo a la cruda)
CAPAS = ["stagedata_03", "stagedata_02", "stagedata_01", "rawdata", "otros"]

_CAPA_RE = re.compile(r"^(rawdata|stagedata_\d{2})_", re.IGNORECASE)
_SUFIJO_DUP_RE = re.compile(r"\s+\d+$")


def capa(flow_name: str) -> str:
    m = _CAPA_RE.match(flow_name or "")
    return m.group(1).lower() if m else "otros"


def norm_tabla(nombre: str | None) -> str | None:
    """'[db].[Tabla]' / '`tabla`' / 'Tabla ' → 'tabla'."""
    if not nombre:
        return None
    t = nombre.strip().replace("`", "").replace("[", "").replace("]", "")
    t = t.split(".")[-1].strip()
    return t.lower() or None


@dataclass
class FuenteRef:
    tabla: str | None          # tabla normalizada, o None si no se pudo determinar
    inferida: bool = False     # True si salió del nombre del nodo y no de source_table
    etiqueta: str = ""         # texto a mostrar


@dataclass
class IndiceLinaje:
    escriben: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    leen: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))


def ref_fuente(source_table, node_name, file_path, tablas_conocidas: set[str]) -> FuenteRef:
    """
    Determina qué tabla lee una fuente. Si no hay source_table, se usa el nombre
    del nodo solo cuando coincide con una tabla que algún flow escribe (evita
    inventar relaciones con nombres de nodo arbitrarios).
    """
    t = norm_tabla(source_table)
    if t:
        return FuenteRef(t, False, t)
    candidato = norm_tabla(_SUFIJO_DUP_RE.sub("", node_name or ""))
    if candidato and candidato in tablas_conocidas:
        return FuenteRef(candidato, True, candidato)
    if file_path:
        return FuenteRef(None, False, file_path.replace("\\", "/").split("/")[-1])
    return FuenteRef(None, False, node_name or "—")


def construir_indice(flows) -> tuple[IndiceLinaje, dict[str, list[FuenteRef]], dict[str, list[str]]]:
    """
    flows: iterable de objetos con flow_name, sources (source_table, node_name,
           file_path) y outputs (destination_table).

    Devuelve (índice, fuentes por flow, tablas escritas por flow).
    """
    escritas: dict[str, list[str]] = {}
    indice = IndiceLinaje()
    for f in flows:
        tablas = sorted({t for o in f.outputs if (t := norm_tabla(o.destination_table))})
        escritas[f.flow_name] = tablas
        for t in tablas:
            indice.escriben[t].append(f.flow_name)

    conocidas = set(indice.escriben)
    fuentes: dict[str, list[FuenteRef]] = {}
    for f in flows:
        refs, vistas = [], set()
        for s in f.sources:
            r = ref_fuente(s.source_table, s.node_name, s.file_path, conocidas)
            clave = r.tabla or r.etiqueta
            if clave in vistas:          # mismo origen leído por varios nodos
                continue
            vistas.add(clave)
            refs.append(r)
            if r.tabla:
                indice.leen[r.tabla].append(f.flow_name)
        fuentes[f.flow_name] = sorted(refs, key=lambda r: r.etiqueta)
    return indice, fuentes, escritas
