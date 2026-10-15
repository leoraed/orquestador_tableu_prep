"""
Acceso al servidor MCP: usuarios con token y auditoría de cada llamada a herramientas.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.catalog.db import BaseCatalog

ROLES = ("usuario", "admin")


class McpUsuario(BaseCatalog):
    __tablename__ = "mcp_usuarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    rol: Mapped[str] = mapped_column(String(20), nullable=False, default="usuario")
    # sha256 del token: el token en claro se muestra una sola vez, al crearlo
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ultimo_uso: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class McpAuditoria(BaseCatalog):
    __tablename__ = "mcp_auditoria"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    usuario: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    herramienta: Mapped[str] = mapped_column(String(100), nullable=False)
    parametros: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # resumen JSON, sin HTML
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    detalle: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    duracion_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
