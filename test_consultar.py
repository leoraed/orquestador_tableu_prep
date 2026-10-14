from dotenv import load_dotenv
load_dotenv(".env")

import pymysql, os

conn = pymysql.connect(
    host=os.getenv("CATALOG_MYSQL_HOST"),
    port=int(os.getenv("CATALOG_MYSQL_PORT", "3306")),
    database=os.getenv("CATALOG_MYSQL_DB"),
    user=os.getenv("CATALOG_MYSQL_USER"),
    password=os.getenv("CATALOG_MYSQL_PASSWORD"),
)
cur = conn.cursor()

sql = """
SELECT
  Provincia,
  DATE_FORMAT(Fecha, '%Y-%m') AS mes,
  COUNT(DISTINCT CASE WHEN Source = 'Ventas' THEN id_direccion END) AS Ventas,
  COUNT(DISTINCT CASE WHEN Source = 'Leads' THEN id_direccion END) AS Leads,
  COUNT(DISTINCT CASE WHEN Source = 'Activacion' THEN id_direccion END) AS Activaciones
FROM stagedata_marketing_link
WHERE Fecha >= '2024-01-01'
GROUP BY Provincia, mes
ORDER BY mes DESC, Ventas DESC
LIMIT 20
"""

cur.execute(sql)
rows = cur.fetchall()
print(f"{'Provincia':<20} {'Mes':<10} {'Ventas':>8} {'Leads':>8} {'Activaciones':>12}")
print("-" * 62)
for r in rows:
    print(f"{str(r[0] or ''):<20} {str(r[1] or ''):<10} {r[2]:>8} {r[3]:>8} {r[4]:>12}")

conn.close()
