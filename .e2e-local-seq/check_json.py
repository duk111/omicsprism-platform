import json, math, pathlib
root=pathlib.Path('.e2e-local-seq/runs')
for job in root.glob('*/job.json'):
 d=json.loads(job.read_text()); print('JOB',d['id'],d['analysis_type'],'status=',d['status'],'dispatch=',d.get('dispatch_state'),'attempt=',d.get('attempt'),'error=',d.get('error'))
 out=job.parent/'outputs'
 for js in out.rglob('*.json'):
  try:
   payload=json.loads(js.read_text(encoding='utf-8'),parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
   print(' JSON_OK',js.relative_to(out))
  except Exception as e: print(' JSON_BAD',js.relative_to(out),repr(e))
