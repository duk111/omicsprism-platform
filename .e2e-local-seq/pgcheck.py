import psycopg
u='postgresql://omics_app:local-app-password@127.0.0.1:5432/omicsprism'
with psycopg.connect(u) as c:
 print(c.execute("select tablename from pg_tables where schemaname='public' order by tablename").fetchall())
