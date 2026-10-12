import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

load_dotenv()

_host = os.getenv("CATALOG_MYSQL_HOST", "localhost")
_port = os.getenv("CATALOG_MYSQL_PORT", "3306")
_db = os.getenv("CATALOG_MYSQL_DB", "bitbi_catalog")
_user = os.getenv("CATALOG_MYSQL_USER", "")
_pass = os.getenv("CATALOG_MYSQL_PASSWORD", "")

CATALOG_URL = f"mysql+pymysql://{_user}:{_pass}@{_host}:{_port}/{_db}?charset=utf8mb4"

engine_catalog = create_engine(CATALOG_URL, pool_pre_ping=True)
SessionCatalog = sessionmaker(bind=engine_catalog, autoflush=False, autocommit=False)


class BaseCatalog(DeclarativeBase):
    pass


def get_catalog_db():
    db = SessionCatalog()
    try:
        yield db
    finally:
        db.close()
