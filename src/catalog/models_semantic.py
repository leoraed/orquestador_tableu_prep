from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, DateTime, Text, JSON, ForeignKey, text
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

    entity: Mapped["SemanticEntity"] = relationship(back_populates="metrics")


def migrar_columnas_semanticas(engine) -> None:
    """Agrega columnas nuevas a tablas existentes (MySQL no tiene ADD COLUMN IF NOT EXISTS)."""
    nuevas = [
        ("semantic_dimensions", "tableau_formula", "TEXT NULL"),
        ("semantic_metrics", "tableau_formula", "TEXT NULL"),
    ]
    with engine.connect() as conn:
        for tabla, columna, ddl in nuevas:
            existe = conn.execute(text(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
            ), {"t": tabla, "c": columna}).scalar()
            if not existe:
                conn.execute(text(f"ALTER TABLE {tabla} ADD COLUMN {columna} {ddl}"))
                conn.commit()
