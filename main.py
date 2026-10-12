import logging
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOG_FILE = BASE_DIR / "logs" / "orquestador.log"

(BASE_DIR / "logs").mkdir(exist_ok=True)
(BASE_DIR / "data").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ],
)

from src.database import engine, Base
from src import models  # registra los modelos en Base
from src.catalog.db import engine_catalog
from src.catalog import models as catalog_models  # registra modelos del catálogo
from src.catalog import models_semantic  # registra modelos semánticos
from sqlalchemy import text

Base.metadata.create_all(bind=engine)
catalog_models.BaseCatalog.metadata.create_all(bind=engine_catalog)

# Migración MySQL: agregar columna graph_data si no existe
with engine_catalog.connect() as conn:
    result = conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'catalog_flows' AND COLUMN_NAME = 'graph_data'"
    ))
    if result.scalar() == 0:
        conn.execute(text("ALTER TABLE catalog_flows ADD COLUMN graph_data JSON NULL"))
        conn.commit()

# Migración: agregar columnas nuevas si no existen (SQLite no soporta IF NOT EXISTS en ALTER)
with engine.connect() as conn:
    existing = [r[1] for r in conn.execute(text("PRAGMA table_info(ejecuciones)")).fetchall()]
    if "grupo_id" not in existing:
        conn.execute(text("ALTER TABLE ejecuciones ADD COLUMN grupo_id VARCHAR(36)"))
        conn.commit()
    if "pipeline_name" not in existing:
        conn.execute(text("ALTER TABLE ejecuciones ADD COLUMN pipeline_name VARCHAR(255)"))
        conn.commit()
    if "step_type" not in existing:
        conn.execute(text("ALTER TABLE ejecuciones ADD COLUMN step_type VARCHAR(30) DEFAULT 'prep'"))
        conn.commit()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api:app", host="0.0.0.0", port=8000, reload=False)
