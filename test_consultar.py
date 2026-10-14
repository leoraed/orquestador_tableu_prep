"""
Agrega mes y % Cierre al modelo_dashboard_marketing y valida el piloto completo.
"""
from dotenv import load_dotenv
load_dotenv(".env")

from src.catalog.db import SessionCatalog
from src.catalog.models_semantic import SemanticModel, SemanticDimension, SemanticMetric

MODELO = "modelo_dashboard_marketing"

db = SessionCatalog()
try:
    m = db.query(SemanticModel).filter(SemanticModel.name == MODELO).first()
    entity = m.entities[0]

    dim_names = {d.name for d in entity.dimensions}
    met_names = {mt.name for mt in entity.metrics}

    if "mes" not in dim_names:
        entity.dimensions.append(SemanticDimension(
            entity_id=entity.id,
            name="mes",
            expression="DATE_FORMAT(Fecha, '%Y-%m')",
            data_type="string",
            description="Mes de la fecha (YYYY-MM)",
        ))
        print("DIM  'mes' agregada.")
    else:
        print("DIM  'mes' ya existe.")

    if "% Cierre" not in met_names:
        entity.metrics.append(SemanticMetric(
            entity_id=entity.id,
            name="% Cierre",
            expression=(
                "COUNT(DISTINCT CASE WHEN Source = 'Ventas' THEN id_direccion END)"
                " / NULLIF(COUNT(DISTINCT CASE WHEN Source = 'Leads' THEN id_direccion END), 0)"
            ),
            format="percent",
            description="Tasa de conversión Ventas / Leads",
        ))
        print("MET  '% Cierre' agregada.")
    else:
        print("MET  '% Cierre' ya existe.")

    db.commit()
    print("Modelo actualizado.\n")
finally:
    db.close()

# Validar piloto completo
from src.catalog.sql_compiler import consultar, ConsultaError

print("--- Piloto: Ventas / Leads / % Cierre por mes (2024) ---")
try:
    r = consultar(
        modelo=MODELO,
        metricas=["Ventas", "Leads", "% Cierre"],
        dimensiones=["mes"],
        periodo={"campo": "Fecha", "desde": "2024-01-01", "hasta": "2024-12-31"},
        limite=12,
    )
    print(r["sql"])
    print(f"\n{r['total']} filas en {r['tiempo_ms']}ms\n")
    print(f"{'Mes':<10} {'Ventas':>8} {'Leads':>8} {'% Cierre':>10}")
    print("-" * 40)
    for row in r["filas"]:
        cierre = f"{float(row[2])*100:.1f}%" if row[2] else "0.0%"
        print(f"{str(row[0]):<10} {row[1]:>8} {row[2]:>8} {cierre:>10}")
except ConsultaError as e:
    print("ConsultaError:", e)
except Exception as e:
    print("Error:", e)
