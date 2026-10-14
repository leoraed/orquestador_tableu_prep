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

for pattern in ["%proxy%", "%marketing%", "%dashboard%", "%sqlproxy%"]:
    cur.execute("SHOW TABLES LIKE %s", (pattern,))
    rows = cur.fetchall()
    if rows:
        print(f"LIKE '{pattern}':", rows)

cur.execute("SHOW FULL TABLES WHERE Table_type = 'VIEW'")
views = cur.fetchall()
print("\nVistas:", views[:20])

conn.close()
