"""
Tableros (dashboards HTML armados con Claude) publicados en el orquestador.
Cada publicación es una versión; el tablero apunta a la última.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.catalog.db import BaseCatalog

# TEXT de MySQL corta en 64 KB; un tablero con datos embebidos puede pesar varios MB
_HTML = Text().with_variant(mysql.LONGTEXT(), "mysql")


class Tablero(BaseCatalog):
    __tablename__ = "tableros"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    titulo: Mapped[str] = mapped_column(String(255), nullable=False)
    descripcion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    modelo: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)   # modelo semántico de origen
    tags: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    tipo: Mapped[str] = mapped_column(String(20), default="foto")              # foto | vivo (paso 3)
    # Usuario MCP que lo creó; None = subido desde la app (sin login todavía)
    autor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    versiones: Mapped[list["TableroVersion"]] = relationship(
        back_populates="tablero", cascade="all, delete-orphan", order_by="TableroVersion.version"
    )

    @property
    def ultima(self) -> Optional["TableroVersion"]:
        return self.versiones[-1] if self.versiones else None


class TableroVersion(BaseCatalog):
    __tablename__ = "tablero_versiones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tablero_id: Mapped[int] = mapped_column(Integer, ForeignKey("tableros.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    html: Mapped[str] = mapped_column(_HTML, nullable=False)
    datos_hasta: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    nota: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)    # qué cambió en esta versión
    autor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    tablero: Mapped["Tablero"] = relationship(back_populates="versiones")
