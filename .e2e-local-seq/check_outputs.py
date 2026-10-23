import json, math, pathlib, httpx, pandas as pd
root=pathlib.Path('.e2e-local-seq/runs')
for job in root.glob('*/job.json'):
 d=json.loads(job.read_text()); print('JOB',d['id'],d['analysis_type'],d['status'],d.get('dispatch_state'),d.get('params'))
 out=job.parent/'outputs'
 for csv in out.glob('*.csv'):
  df=pd.read_csv(csv)
  nums=df.select_dtypes('number')
  finite=bool(nums.empty or nums.map(lambda x: math.isfinite(float(x))).all().all())
  print(' CSV',csv.name,df.shape,'finite',finite,'cols',list(df.columns)[:8])
 for js in out.rglob('*.json'):
  try: json.loads(js.read_text(),parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
  except Exception as e: print(' BADJSON',js,e)
# Permission check against a completed job id using a fresh client (no session cookie).
ids=[json.loads(p.read_text())['id'] for p in root.glob('*/job.json')]
with httpx.Client(base_url='http://127.0.0.1:18087',timeout=10) as c:
 for jid in ids:
  r=c.get('/api/jobs/'+jid); print('CROSS_SESSION_JOB',jid,r.status_code,r.text[:180])
