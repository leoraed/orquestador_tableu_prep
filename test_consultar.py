from dotenv import load_dotenv
load_dotenv(".env")

from src.catalog.sql_compiler import consultar, ConsultaError

try:
    r = consultar("modelo_dashboard_marketing", ["Ventas"], ["Provincia"], limite=5)
    print("SQL:", r["sql"])
    print("Filas:", r["filas"])
except ConsultaError as e:
    print("ConsultaError:", e)
except Exception as e:
    print("Error:", e)
