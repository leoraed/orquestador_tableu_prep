from dotenv import load_dotenv
load_dotenv(".env")

from src.catalog.sql_compiler import consultar, ConsultaError

try:
    r = consultar(
        modelo="modelo_dashboard_marketing",
        metricas=["Ventas", "Leads"],
        dimensiones=["Provincia"],
        periodo={"campo": "mes", "desde": "2024-01", "hasta": "2024-12"},
        limite=50,
    )
    print(r["sql"])
    print(f"\n{r['total']} filas en {r['tiempo_ms']}ms\n")
    print(f"{'Provincia':<25} {'Ventas':>8} {'Leads':>8}")
    print("-" * 44)
    for row in r["filas"]:
        print(f"{str(row[0] or '(null)'):<25} {row[1]:>8} {row[2]:>8}")
except ConsultaError as e:
    print("ConsultaError:", e)
except Exception as e:
    print("Error:", e)
