"""
Operaciones sobre modelos semánticos completos: serializar, backups,
restaurar y reimportar un .twb sin pisar lo editado a mano.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from src.catalog.models_semantic import (
    SemanticDimension, SemanticEntity, SemanticMetric, SemanticModel, SemanticModelBackup,
)

# Backups automáticos que se conservan por modelo (los manuales no se borran)
MAX_BACKUPS_AUTOMATICOS = 30


# ── Serialización (mismo formato que el export YAML) ─────────────────────────

def modelo_a_dict(m: SemanticModel) -> dict:
    return {
        "name": m.name,
        "description": m.description or "",
        "entities": [
            {
                "name": e.name,
                "description": e.description or "",
                "base_table": e.base_table,
                "joins": e.joins or [],
                "dimensions": [
                    {"name": d.name, "expression": d.expression,
                     "data_type": d.data_type or "string", "description": d.description or "",
                     "tableau_formula": d.tableau_formula, "editado": d.editado}
                    for d in e.dimensions
                ],
                "metrics": [
                    {"name": mt.name, "expression": mt.expression,
                     "format": mt.format or "number", "description": mt.description or "",
                     "tableau_formula": mt.tableau_formula, "editado": mt.editado}
                    for mt in e.metrics
                ],
            }
            for e in m.entities
        ],
    }


def guardar_entidades(db: Session, modelo: SemanticModel, entities_data: list) -> None:
    for e_data in entities_data:
        entity = SemanticEntity(
            model_id=modelo.id,
            name=(e_data.get("name") or "").strip(),
            description=(e_data.get("description") or "").strip() or None,
            base_table=(e_data.get("base_table") or "").strip(),
            joins=e_data.get("joins") or [],
        )
        db.add(entity)
        db.flush()
        for d in e_data.get("dimensions") or []:
            db.add(SemanticDimension(
                entity_id=entity.id,
                name=(d.get("name") or "").strip(),
                expression=(d.get("expression") or "").strip(),
                data_type=d.get("data_type") or "string",
                description=(d.get("description") or "").strip() or None,
                tableau_formula=d.get("tableau_formula"),
                editado=d.get("editado"),
            ))
        for mt in e_data.get("metrics") or []:
            db.add(SemanticMetric(
                entity_id=entity.id,
                name=(mt.get("name") or "").strip(),
                expression=(mt.get("expression") or "").strip(),
                format=mt.get("format") or "number",
                description=(mt.get("description") or "").strip() or None,
                tableau_formula=mt.get("tableau_formula"),
                editado=mt.get("editado"),
            ))


def validar_contenido(contenido) -> str | None:
    """Chequeo mínimo de un modelo serializado (ej. un YAML subido)."""
    if not isinstance(contenido, dict) or not str(contenido.get("name") or "").strip():
        return "El archivo no es un modelo válido: falta «name»."
    entidades = contenido.get("entities")
    if not isinstance(entidades, list):
        return "El archivo no es un modelo válido: falta la lista «entities»."
    for e in entidades:
        if not isinstance(e, dict) or not e.get("name"):
            return "Hay una entidad sin nombre."
        for kind in ("dimensions", "metrics"):
            for c in e.get(kind) or []:
                if not isinstance(c, dict) or not c.get("name") or not c.get("expression"):
                    return f"Hay un campo sin nombre o sin expresión en «{e['name']}»."
    return None


# ── Backups ──────────────────────────────────────────────────────────────────

def crear_backup(db: Session, modelo: SemanticModel, motivo: str,
                 automatico: bool = True) -> SemanticModelBackup:
    b = SemanticModelBackup(model_name=modelo.name, motivo=motivo[:255], automatico=automatico,
                            contenido=modelo_a_dict(modelo), created_at=datetime.utcnow())
    db.add(b)
    db.flush()
    if automatico:
        viejos = (db.query(SemanticModelBackup)
                  .filter(SemanticModelBackup.model_name == modelo.name,
                          SemanticModelBackup.automatico.is_(True))
                  .order_by(SemanticModelBackup.created_at.desc(), SemanticModelBackup.id.desc())
                  .offset(MAX_BACKUPS_AUTOMATICOS).all())
        for v in viejos:
            db.delete(v)
    return b


def resumen_backup(b: SemanticModelBackup) -> dict:
    ents = b.contenido.get("entities") or []
    return {
        "id": b.id,
        "model_name": b.model_name,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "motivo": b.motivo,
        "automatico": bool(b.automatico),
        "campos": sum(len(e.get("dimensions") or []) + len(e.get("metrics") or []) for e in ents),
    }


def restaurar(db: Session, contenido: dict, nombre: str, motivo: str) -> SemanticModel:
    """
    Reemplaza el modelo `nombre` por `contenido` (lo crea si no existe).
    Si existe, antes guarda un backup automático del estado actual.
    """
    modelo = db.query(SemanticModel).filter(SemanticModel.name == nombre).first()
    if modelo:
        crear_backup(db, modelo, motivo)
        for e in list(modelo.entities):
            db.delete(e)
        db.flush()
    else:
        modelo = SemanticModel(name=nombre, created_at=datetime.utcnow())
        db.add(modelo)
        db.flush()
    modelo.description = (contenido.get("description") or "").strip() or None
    modelo.updated_at = datetime.utcnow()
    guardar_entidades(db, modelo, contenido.get("entities") or [])
    db.flush()
    db.refresh(modelo)
    return modelo


# ── Reimportar un .twb sobre un modelo existente ─────────────────────────────

def _editado(c) -> bool:
    # None = campo anterior a la marca: no se sabe si se editó, se protege
    return c.editado is not False


def reimportar(db: Session, modelo: SemanticModel, ds, aplicar: bool) -> dict:
    """
    Fusiona un datasource parseado (twb_parser.TwbDatasource) con el modelo:
    - Campos que nadie editó: se actualizan con lo que trae el .twb.
    - Campos editados a mano (o sin marca): se conservan; solo se refresca la
      fórmula original de Tableau como referencia.
    - Campos nuevos del .twb: se agregan.
    - Campos que ya no están en el .twb: se borran si nadie los editó; si no,
      se conservan y se informan.
    - Tabla base y joins: se conservan; la tabla base solo se completa si estaba vacía.

    Con aplicar=False no modifica nada y devuelve el plan.
    """
    plan = {"actualizados": [], "agregados": [], "eliminados": [], "conservados": [],
            "sin_cambios": 0, "entidad": []}
    entity = next((e for e in modelo.entities if e.name == ds.caption), None) or \
        (modelo.entities[0] if modelo.entities else None)
    if entity is None:
        entity = SemanticEntity(model_id=modelo.id, name=ds.caption, description=ds.description,
                                base_table=ds.base_table or "", joins=[])
        plan["entidad"].append(f"Se crea la entidad «{ds.caption}».")
        if aplicar:
            db.add(entity)
            db.flush()
    elif not entity.base_table and ds.base_table:
        plan["entidad"].append(f"Se completa la tabla base: {ds.base_table}.")
        if aplicar:
            entity.base_table = ds.base_table
    else:
        plan["entidad"].append("Tabla base y joins: se conservan.")

    for kind, nuevos, actuales, Clase, attr in (
        ("dimension", ds.dimensions, list(entity.dimensions or []), SemanticDimension, "data_type"),
        ("metric", ds.metrics, list(entity.metrics or []), SemanticMetric, "format"),
    ):
        por_nombre = {c.name: c for c in actuales}
        vistos = set()
        for n in nuevos:
            vistos.add(n["name"])
            ref = {"name": n["name"], "kind": kind}
            valor_attr = n.get(attr) or ("string" if attr == "data_type" else "number")
            c = por_nombre.get(n["name"])
            if c is None:
                plan["agregados"].append(ref)
                if aplicar:
                    db.add(Clase(entity_id=entity.id, name=n["name"], expression=n["expression"],
                                 description=n.get("description") or None,
                                 tableau_formula=n.get("tableau_formula"), editado=False,
                                 **{attr: valor_attr}))
                continue
            igual = c.expression == n["expression"] and getattr(c, attr) == valor_attr
            if igual:
                plan["sin_cambios"] += 1
            elif _editado(c):
                plan["conservados"].append({**ref, "motivo": "editado a mano" if c.editado
                                            else "sin marca de edición (anterior a esta función)"})
            else:
                plan["actualizados"].append(ref)
                if aplicar:
                    c.expression = n["expression"]
                    setattr(c, attr, valor_attr)
            if aplicar:
                c.tableau_formula = n.get("tableau_formula") or c.tableau_formula

        for c in actuales:
            if c.name in vistos:
                continue
            ref = {"name": c.name, "kind": kind}
            if _editado(c):
                plan["conservados"].append({**ref, "motivo": "ya no está en el workbook (se conserva)"})
            else:
                plan["eliminados"].append(ref)
                if aplicar:
                    db.delete(c)

    if aplicar:
        modelo.updated_at = datetime.utcnow()
        db.flush()
    return plan
