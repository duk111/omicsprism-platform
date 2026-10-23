import json, time, httpx
base='http://127.0.0.1:18087'
counts='gene,s1,s2,s3,s4,s5,s6\ngene_up,100,102,101,420,430,425\ngene_stable,200,201,199,202,203,201\ngene_low,40,41,39,42,40,41\ngene_mid,80,82,81,85,84,86\ngene_other,60,61,62,63,64,65\n'
metadata='sample_id,treatment\ns1,control\ns2,control\ns3,control\ns4,salt\ns5,salt\ns6,salt\n'
with httpx.Client(base_url=base, timeout=30.0) as c:
 r=c.post('/api/jobs/preflight',data={'analysis_type':'deg','compare_field':'treatment','tested_levels':'salt','reference_level':'control','min_total_count':'0','min_replicates':'2','padj_cutoff':'1','log2fc_cutoff':'0'},files={'counts':('counts.csv',counts,'text/csv'),'metadata':('metadata.csv',metadata,'text/csv')}); print('preflight',r.status_code,r.json().get('can_submit'))
 r=c.post('/api/jobs',data={'analysis_type':'deg','compare_field':'treatment','tested_levels':'salt','reference_level':'control','min_total_count':'0','min_replicates':'2','padj_cutoff':'1','log2fc_cutoff':'0'},files={'counts':('counts.csv',counts,'text/csv'),'metadata':('metadata.csv',metadata,'text/csv')}); print('create',r.status_code,r.text[:500]); r.raise_for_status(); job=r.json(); jid=job['id']; print('jid',jid,'cookie',c.cookies)
 for i in range(120):
  time.sleep(1); q=c.get(f'/api/jobs/{jid}'); data=q.json(); print(i+1,data.get('status'),data.get('progress'))
  if data.get('status') in ('succeeded','failed','cancelled'): break
 f=c.get(f'/api/jobs/{jid}/files'); print('files',f.status_code); print(json.dumps(f.json(),indent=2)[:5000])
 if data.get('status')=='succeeded':
  csv_file=next((x for x in f.json()['result_files'] if x['path'].endswith('.all.csv')),None); print('csv',csv_file)
  if csv_file:
   d=c.get(f"/api/jobs/{jid}/download/{csv_file['path']}"); print('download',d.status_code,d.text[:800])
