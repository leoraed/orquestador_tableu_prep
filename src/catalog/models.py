from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, DateTime, Text, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.catalog.db import BaseCatalog


class CatalogFlow(BaseCatalog):
    __tablename__ = "catalog_flows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    file_path: Mapped[str] = mapped_column(String(500))
    parsed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    node_count: Mapped[int] = mapped_column(Integer, default=0)

    sources: Mapped[list["CatalogSource"]] = relationship(
        back_populates="flow", cascade="all, delete-orphan"
    )
    outputs: Mapped[list["CatalogOutput"]] = relationship(
        back_populates="flow", cascade="all, delete-orphan"
    )
    transforms: Mapped[list["CatalogTransformation"]] = relationship(
        back_populates="flow", cascade="all, delete-orphan"
    )


class CatalogSource(BaseCatalog):
    __tablename__ = "catalog_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_id: Mapped[int] = mapped_column(Integer, ForeignKey("catalog_flows.id"), nullable=False)
    node_id: Mapped[str] = mapped_column(String(36))
    node_name: Mapped[str] = mapped_column(String(255))
    source_type: Mapped[str] = mapped_column(String(50))   # mysql, excel, googledrive, etc.
    source_table: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    server: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    database_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    file_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    flow: Mapped["CatalogFlow"] = relationship(back_populates="sources")
    fields: Mapped[list["CatalogField"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class CatalogField(BaseCatalog):
    __tablename__ = "catalog_fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(Integer, ForeignKey("catalog_sources.id"), nullable=False)
    flow_id: Mapped[int] = mapped_column(Integer, ForeignKey("catalog_flows.id"), nullable=False)
    field_name: Mapped[str] = mapped_column(String(255))
    field_type: Mapped[str] = mapped_column(String(50))
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    # Enriquecimiento manual
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    source: Mapped["CatalogSource"] = relationship(back_populates="fields")


class CatalogOutput(BaseCatalog):
    __tablename__ = "catalog_outputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_id: Mapped[int] = mapped_column(Integer, ForeignKey("catalog_flows.id"), nullable=False)
    node_id: Mapped[str] = mapped_column(String(36))
    node_name: Mapped[str] = mapped_column(String(255))
    destination_table: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    flow: Mapped["CatalogFlow"] = relationship(back_populates="outputs")


class CatalogTransformation(BaseCatalog):
    __tablename__ = "catalog_transformations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_id: Mapped[int] = mapped_column(Integer, ForeignKey("catalog_flows.id"), nullable=False)
    node_id: Mapped[str] = mapped_column(String(36))
    node_name: Mapped[str] = mapped_column(String(255))
    transform_type: Mapped[str] = mapped_column(String(50))  # join, aggregate, clean
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    flow: Mapped["CatalogFlow"] = relationship(back_populates="transforms")
