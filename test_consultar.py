"""
Actualiza las expresiones del piloto en modelo_dashboard_marketing.
Ejecutar una sola vez después de confirmar que el SQL funciona.
"""
from dotenv import load_dotenv
load_dotenv(".env")

from src.catalog.db import SessionCatalog
from src.catalog.models_semantic import SemanticModel, SemanticDimension, SemanticMetric

MODELO = "modelo_dashboard_marketing"
BASE_TABLE = "stagedata_marketing_link"

DIMS = {
    "Provincia":  "Provincia",
    "mes":        "DATE_FORMAT(Fecha, '%Y-%m')",
    "Pais":       "Pais",
    "Fecha":      "Fecha",
    "segmento":   "segmento",
    "Descuento":  "Descuento",
    "cliente_id": "cliente_id",
}

METS = {
    "Ventas":       "COUNT(DISTINCT CASE WHEN Source = 'Ventas' THEN id_direccion END)",
    "Leads":        "COUNT(DISTINCT CASE WHEN Source = 'Leads' THEN id_direccion END)",
    "Activaciones": "COUNT(DISTINCT CASE WHEN Source = 'Activacion' THEN id_direccion END)",
}

db = SessionCatalog()
try:
    m = db.query(SemanticModel).filter(SemanticModel.name == MODELO).first()
    if not m:
        print(f"Modelo '{MODELO}' no encontrado.")
        exit(1)

    entity = m.entities[0]

    # Actualizar tabla base
    entity.base_table = BASE_TABLE
    print(f"base_table → {BASE_TABLE}")

    # Actualizar dimensiones
    for d in entity.dimensions:
        if d.name in DIMS:
            old = d.expression
            d.expression = DIMS[d.name]
            print(f"  DIM  {d.name!r}: {old!r} → {d.expression!r}")

    # Actualizar métricas
    for mt in entity.metrics:
        if mt.name in METS:
            old = mt.expression
            mt.expression = METS[mt.name]
            print(f"  MET  {mt.name!r}: {old!r} → {mt.expression!r}")

    db.commit()
    print("\nListo. Modelo actualizado.")
finally:
    db.close()

# Verificar con consultar()
print("\n--- Test consultar() ---")
from src.catalog.sql_compiler import consultar, ConsultaError
try:
    r = consultar(
        modelo=MODELO,
        metricas=["Ventas", "Leads"],
        dimensiones=["Provincia"],
        limite=10,
    )
    print(r["sql"])
    print(f"{r['total']} filas en {r['tiempo_ms']}ms")
    for row in r["filas"]:
        print(row)
except ConsultaError as e:
    print("ConsultaError:", e)
except Exception as e:
    print("Error:", e)
