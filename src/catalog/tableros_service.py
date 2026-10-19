"""
Publicación de tableros: lo usan la API de la app (subida desde la UI) y el
servidor MCP (publicación directa desde Claude).
"""
import re
import unicodedata
from datetime import date, datetime

from sqlalchemy.orm import Session

from src.catalog.models_tableros import Tablero, TableroVersion

MAX_BYTES = 10 * 1024 * 1024
# El HTML de un tablero ejecuta JavaScript propio: se sirve aislado (sin acceso a la
# app, sus cookies ni su API). Sin allow-same-origin el documento queda en un origen opaco.
SANDBOX = "allow-scripts allow-popups allow-downloads"


class TableroError(ValueError):
    pass


def slugify(texto: str) -> str:
    s = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:100] or "tablero"


def _slug_unico(db: Session, base: str) -> str:
    slug, n = base, 2
    while db.query(Tablero).filter(Tablero.slug == slug).first():
        slug, n = f"{base}-{n}", n + 1
    return slug


def validar_html(html: str) -> None:
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise TableroError("El HTML supera los 10 MB.")
    if "<" not in html:
        raise TableroError("El contenido no parece HTML.")


def parse_fecha(valor: str | None) -> date | None:
    if not valor or not valor.strip():
        return None
    try:
        return date.fromisoformat(valor.strip())
    except ValueError:
        raise TableroError("La fecha de los datos tiene que ser AAAA-MM-DD.")


def _tags(valor: str | None) -> str | None:
    return ", ".join(x.strip() for x in (valor or "").split(",") if x.strip()) or None


MAX_TEXTO = 4000


def _texto(valor: str | None) -> str | None:
    """Pedido / decisiones: texto libre, recortado."""
    valor = (valor or "").strip()
    return valor[:MAX_TEXTO] if valor else None


def buscar(db: Session, slug: str) -> Tablero | None:
    return db.query(Tablero).filter(Tablero.slug == slug).first()


def crear(db: Session, titulo: str, html: str, descripcion: str = "", modelo: str = "",
          tags: str = "", datos_hasta: str = "", nota: str = "Primera versión",
          autor: str | None = None, receta: dict | None = None, pedido: str = "",
          decisiones: str = "") -> Tablero:
    """receta: ya validada (tableros_vivos.validar_receta); con receta el tablero es «vivo»."""
    titulo = (titulo or "").strip()
    if not titulo:
        raise TableroError("El título es obligatorio.")
    validar_html(html)
    fecha = parse_fecha(datos_hasta)
    ahora = datetime.utcnow()
    t = Tablero(slug=_slug_unico(db, slugify(titulo)), titulo=titulo,
                descripcion=(descripcion or "").strip() or None, modelo=(modelo or "").strip() or None,
                tags=_tags(tags), autor=autor, tipo="vivo" if receta else "foto",
                created_at=ahora, updated_at=ahora)
    t.versiones.append(TableroVersion(version=1, html=html, datos_hasta=fecha, autor=autor, receta=receta,
                                      nota=(nota or "").strip()[:500] or None, created_at=ahora,
                                      pedido=_texto(pedido), decisiones=_texto(decisiones)))
    db.add(t)
    db.flush()
    return t


def nueva_version(db: Session, t: Tablero, html: str, datos_hasta: str = "", nota: str = "",
                  autor: str | None = None, receta: dict | None = None, pedido: str = "",
                  decisiones: str = "") -> Tablero:
    validar_html(html)
    fecha = parse_fecha(datos_hasta)
    ahora = datetime.utcnow()
    t.versiones.append(TableroVersion(version=(t.ultima.version if t.ultima else 0) + 1, html=html,
                                      datos_hasta=fecha, nota=(nota or "").strip()[:500] or None,
                                      autor=autor, receta=receta, created_at=ahora,
                                      pedido=_texto(pedido), decisiones=_texto(decisiones)))
    t.tipo = "vivo" if receta else "foto"
    t.updated_at = ahora
    db.flush()
    return t


def puede_publicar(t: Tablero, usuario: str, es_admin: bool) -> bool:
    """Por el MCP solo el autor (o un admin) publica versiones nuevas o edita un tablero.
    Los subidos desde la app (sin autor) solo los modifican admins."""
    return es_admin or (t.autor is not None and t.autor == usuario)


def editar(t: Tablero, titulo: str, descripcion: str = "", modelo: str = "", tags: str = "") -> Tablero:
    titulo = (titulo or "").strip()
    if not titulo:
        raise TableroError("El título es obligatorio.")
    t.titulo = titulo
    t.descripcion = (descripcion or "").strip() or None
    t.modelo = (modelo or "").strip() or None
    t.tags = _tags(tags)
    t.updated_at = datetime.utcnow()
    return t


def a_dict(t: Tablero) -> dict:
    u = t.ultima
    return {
        "slug": t.slug, "titulo": t.titulo, "descripcion": t.descripcion or "",
        "modelo": t.modelo or "", "tags": [x.strip() for x in (t.tags or "").split(",") if x.strip()],
        "tipo": t.tipo, "version": u.version if u else 0, "autor": t.autor,
        "datos_hasta": u.datos_hasta.isoformat() if u and u.datos_hasta else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "versiones": [{"version": v.version, "nota": v.nota or "", "autor": v.autor,
                       "tipo": "vivo" if v.receta else "foto", "receta": v.receta,
                       "pedido": v.pedido or "", "decisiones": v.decisiones or "",
                       "datos_hasta": v.datos_hasta.isoformat() if v.datos_hasta else None,
                       "created_at": v.created_at.isoformat() if v.created_at else None,
                       "bytes": len(v.html.encode("utf-8"))} for v in reversed(t.versiones)],
    }
