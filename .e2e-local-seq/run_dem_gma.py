import json,time,httpx
base='http://127.0.0.1:18087'
def run(kind, files, data):
 with httpx.Client(base_url=base,timeout=30.0) as c:
  p=c.post('/api/jobs/preflight',data={'analysis_type':kind,**data},files=files); print(kind,'preflight',p.status_code,p.json().get('can_submit'),p.text[:300])
  q=c.post('/api/jobs',data={'analysis_type':kind,**data},files=files); print(kind,'create',q.status_code,q.text[:300]); q.raise_for_status(); jid=q.json()['id']; print(kind,'jid',jid)
  for i in range(180):
   time.sleep(1); s=c.get('/api/jobs/'+jid).json();
   if i<3 or i%10==9 or s.get('status') in ('succeeded','failed','cancelled'): print(kind,'poll',i+1,s.get('status'),s.get('progress'))
   if s.get('status') in ('succeeded','failed','cancelled'): break
  fs=c.get('/api/jobs/'+jid+'/files'); print(kind,'files',fs.status_code,'result_count',len(fs.json().get('result_files',[])))
  if s.get('status')=='succeeded':
   nums=[]
   for x in fs.json()['result_files']:
    if x['path'].endswith('.all.csv') or x['path'].endswith('.all.csv'):
     d=c.get('/api/jobs/'+jid+'/download/'+x['path']); print(kind,'table',x['path'],d.status_code,d.text[:250]); nums.append(x['path'])
   for fid in ('volcano','pca'):
    d=c.get(f'/api/jobs/{jid}/figure-data/{fid}');
    if d.status_code!=404: print(kind,'figure',fid,d.status_code,d.text[:180])
  return jid
metadata='sample_id,treatment\ns1,control\ns2,control\ns3,control\ns4,salt\ns5,salt\ns6,salt\n'
metabs='metabolite,s1,s2,s3,s4,s5,s6\nmetab_up,10,11,12,80,82,81\nmetab_stable,25,26,24,25,26,24\nmetab_missing,5,,7,9,10,11\nmetab_flat,40,40,40,40,40,40\nmetab_high,100,102,101,104,103,105\nmetab_low,2,3,2,4,3,4\n'
run('dem',{'metabs':('metabs.csv',metabs,'text/csv'),'metadata':('metadata.csv',metadata,'text/csv')},{'compare_field':'treatment','tested_levels':'salt','reference_level':'control','min_replicates':'2','vip_cutoff':'0','padj_cutoff':'1','log2fc_cutoff':'0','max_missing_fraction':'0.5','n_orthogonal_components':'0'})
trans='gene,'+','.join(f's{i}' for i in range(1,9))+'\n'+'\n'.join([f'g{i},'+','.join(str(2*i+1+(j%3)*0.2) for j in range(8)) for i in range(1,13)])+'\n'
met='metabolite,'+','.join(f's{i}' for i in range(1,9))+'\n'+'\n'.join([f'm{i},'+','.join(str(3*i+1+(j%4)*0.3) for j in range(8)) for i in range(1,7)])+'\n'
group='sample_id,group1,group2\n'+'\n'.join(f's{i},{"control" if i<=4 else "salt"},{"b1" if i%2 else "b2"}' for i in range(1,9))+'\n'
run('gma',{'transcriptome':('transcriptome.csv',trans,'text/csv'),'metabolome':('metabolome.csv',met,'text/csv'),'group':('group.csv',group,'text/csv')},{'fdr_cutoff':'0.5','enable_modules':'false','max_missing_fraction':'0.5','trans_log2':'false','metab_log2':'false'})
