import psycopg
with psycopg.connect('postgresql://omics_app:local-app-password@127.0.0.1:5432/omicsprism') as c:
 for jid in ['bf756f6c-3863-4fdc-8db7-4bb18db49c12','66ef7696-d700-42c5-b674-ceb89ba2ad3d','d91e7af6-1662-4372-85f1-fb458cbfae9c']:
  row=c.execute('select id,analysis_type,status,payload from jobs where id=%s',(jid,)).fetchone(); print(row[:3], str(row[3])[:300])
