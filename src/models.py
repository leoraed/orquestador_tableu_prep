from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column
from src.database import Base


class EjecucionFlow(Base):
    __tablename__ = "ejecuciones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre_flow: Mapped[str] = mapped_column(String(255), index=True)
    archivo_flow: Mapped[str] = mapped_column(String(500))
    inicio: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    fin: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    estado: Mapped[str] = mapped_column(String(20))  # en_proceso | exitoso | fallido
    salida: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    disparador: Mapped[str] = mapped_column(String(20), default="scheduler")  # scheduler | manual | dependencia
    grupo_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
