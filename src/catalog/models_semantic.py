from datetime import datetime
from typing import Optional
from sqlalchemy import Boolean, Integer, String, DateTime, Text, JSON, ForeignKey, text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.catalog.db import BaseCatalog


class SemanticModel(BaseCatalog):
    __tablename__ = "semantic_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    entities: Mapped[list["SemanticEntity"]] = relationship(
        back_populates="model", cascade="all, delete-orphan", order_by="SemanticEntity.id"
    )


class SemanticEntity(BaseCatalog):
    __tablename__ = "semantic_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(Integer, ForeignKey("semantic_models.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    base_table: Mapped[str] = mapped_column(String(255), nullable=False)
    # Origen como consulta SQL (ej. una consulta personalizada de Tableau): FROM (base_sql) AS `base_table`
    base_sql: Mapped[Optional[str]] = mapped_column(Text().with_variant(mysql.MEDIUMTEXT(), "mysql"), nullable=True)
    # Cada join: {table, alias, on, type} y opcionalmente sql (JOIN (sql) AS `alias`)
    joins: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    model: Mapped["SemanticModel"] = relationship(back_populates="entities")
    dimensions: Mapped[list["SemanticDimension"]] = relationship(
        back_populates="entity", cascade="all, delete-orphan", order_by="SemanticDimension.id"
    )
    metrics: Mapped[list["SemanticMetric"]] = relationship(
        back_populates="entity", cascade="all, delete-orphan", order_by="SemanticMetric.id"
    )


class SemanticDimension(BaseCatalog):
    __tablename__ = "semantic_dimensions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int] = mapped_column(Integer, ForeignKey("semantic_entities.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expression: Mapped[str] = mapped_column(Text, nullable=False)
    data_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    tableau_formula: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # True: editado a mano (reimportar no lo pisa). False: tal cual lo dejó el
    # importador. None: anterior a esta columna (se trata como editado).
    editado: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    entity: Mapped["SemanticEntity"] = relationship(back_populates="dimensions")


class SemanticMetric(BaseCatalog):
    __tablename__ = "semantic_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int] = mapped_column(Integer, ForeignKey("semantic_entities.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expression: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    tableau_formula: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # True: editado a mano (reimportar no lo pisa). False: tal cual lo dejó el
    # importador. None: anterior a esta columna (se trata como editado).
    editado: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    entity: Mapped["SemanticEntity"] = relationship(back_populates="metrics")


class SemanticModelBackup(BaseCatalog):
    """Copia completa de un modelo (mismo formato que el export YAML)."""
    __tablename__ = "semantic_model_backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Por nombre y sin FK: el backup sobrevive aunque se borre el modelo
    model_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    motivo: Mapped[str] = mapped_column(String(255), nullable=False)
    automatico: Mapped[bool] = mapped_column(Boolean, default=True)
    contenido: Mapped[dict] = mapped_column(JSON, nullable=False)


def migrar_columnas_semanticas(engine) -> None:
    """Agrega columnas nuevas a tablas existentes (MySQL no tiene ADD COLUMN IF NOT EXISTS)."""
    nuevas = [
        ("semantic_dimensions", "tableau_formula", "TEXT NULL"),
        ("semantic_metrics", "tableau_formula", "TEXT NULL"),
        ("semantic_dimensions", "editado", "TINYINT(1) NULL"),
        ("semantic_metrics", "editado", "TINYINT(1) NULL"),
        ("tableros", "autor", "VARCHAR(255) NULL"),
        ("tablero_versiones", "autor", "VARCHAR(255) NULL"),
        ("tablero_versiones", "receta", "JSON NULL"),
        ("tablero_versiones", "pedido", "TEXT NULL"),
        ("tablero_versiones", "decisiones", "TEXT NULL"),
        ("semantic_entities", "base_sql", "MEDIUMTEXT NULL"),
    ]
    with engine.connect() as conn:
        for tabla, columna, ddl in nuevas:
            tabla_existe = conn.execute(text(
                "SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
            ), {"t": tabla}).scalar()
            if not tabla_existe:   # create_all la crea completa
                continue
            existe = conn.execute(text(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
            ), {"t": tabla, "c": columna}).scalar()
            if not existe:
                conn.execute(text(f"ALTER TABLE {tabla} ADD COLUMN {columna} {ddl}"))
                conn.commit()
