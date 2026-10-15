"""
Usuarios de la app (login + token del MCP) y auditoría.

McpUsuario / McpAuditoria son las tablas de la primera versión (solo MCP); sus
usuarios se migran a `usuarios` al arrancar (src.usuarios.migrar_usuarios_mcp).
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


class Usuario(BaseCatalog):
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    usuario: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)  # para el login
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)                            # visible, autoría
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    roles: Mapped[str] = mapped_column(String(255), nullable=False, default="lector")           # separados por coma
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    debe_cambiar_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Se incrementa al cambiar la contraseña o desactivar: invalida las sesiones abiertas
    sesion_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    mcp_token_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True, index=True)
    mcp_token_creado: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ultimo_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ultimo_uso_mcp: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Auditoria(BaseCatalog):
    """Acciones de la app (pedidos que modifican algo, logins) y llamadas a herramientas del MCP."""
    __tablename__ = "auditoria"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    origen: Mapped[str] = mapped_column(String(10), nullable=False)          # app | mcp
    usuario: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    accion: Mapped[str] = mapped_column(String(255), nullable=False)         # "POST /api/..." o herramienta
    parametros: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # resumen, nunca contraseñas ni HTML
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    detalle: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    duracion_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
