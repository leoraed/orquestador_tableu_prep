import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from src.catalog.parser import parse_tfl
from src.catalog.models import CatalogFlow, CatalogSource, CatalogField, CatalogOutput, CatalogTransformation

logger = logging.getLogger(__name__)


def save_parsed_flow(data: dict, db: Session) -> CatalogFlow:
    existing = db.query(CatalogFlow).filter(CatalogFlow.file_path == data["file_path"]).first()
    if existing:
        db.delete(existing)
        db.flush()

    flow = CatalogFlow(
        flow_name=data["flow_name"],
        file_path=data["file_path"],
        parsed_at=datetime.utcnow(),
        node_count=data["node_count"],
        graph_data=data.get("graph"),
    )
    db.add(flow)
    db.flush()

    for src in data["sources"]:
        source = CatalogSource(
            flow_id=flow.id,
            node_id=src["node_id"],
            node_name=src["node_name"],
            source_type=src["source_type"],
            source_table=src["source_table"],
            server=src["server"],
            database_name=src["database_name"],
            file_path=src["file_path"],
        )
        db.add(source)
        db.flush()

        for f in src["fields"]:
            db.add(CatalogField(
                source_id=source.id,
                flow_id=flow.id,
                field_name=f["name"],
                field_type=f["type"],
                ordinal=f["ordinal"],
            ))

    for out in data["outputs"]:
        db.add(CatalogOutput(
            flow_id=flow.id,
            node_id=out["node_id"],
            node_name=out["node_name"],
            destination_table=out["destination_table"],
        ))

    for t in data["transforms"]:
        db.add(CatalogTransformation(
            flow_id=flow.id,
            node_id=t["node_id"],
            node_name=t["node_name"],
            transform_type=t["transform_type"],
            details=t["details"],
        ))

    db.commit()
    db.refresh(flow)
    return flow


def parse_and_save(file_path: Path, db: Session) -> dict:
    try:
        data = parse_tfl(file_path)
        flow = save_parsed_flow(data, db)
        logger.info(f"Parseado: {file_path.name} → {len(data['sources'])} fuentes, {len(data['outputs'])} outputs")
        return {
            "file": file_path.name,
            "flow_name": flow.flow_name,
            "sources": len(data["sources"]),
            "outputs": len(data["outputs"]),
            "status": "ok",
        }
    except Exception as e:
        logger.error(f"Error parseando {file_path.name}: {e}")
        return {"file": file_path.name, "status": "error", "error": str(e)}


def parse_carpeta(carpeta: str, db: Session) -> list[dict]:
    results = []
    for tfl in sorted(Path(carpeta).glob("*.tfl")):
        results.append(parse_and_save(tfl, db))
    return results


def parse_todas_las_carpetas(carpetas: list[str], db: Session) -> list[dict]:
    results = []
    for carpeta in carpetas:
        results.extend(parse_carpeta(carpeta, db))
    return results
