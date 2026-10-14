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

cur.execute("DESCRIBE stagedata_marketing_link")
print("=== stagedata_marketing_link ===")
for row in cur.fetchall():
    print(row)

print()
cur.execute("SELECT DISTINCT Source FROM stagedata_marketing_link LIMIT 30")
print("=== Valores de Source ===")
for row in cur.fetchall():
    print(row)

conn.close()
