## 1、初始化数据库PostgreSQL
cd D:\JetBrains\PythonProjects\omicsprism-platform

$env:PGPASSWORD = "white0722"

& "D:\PostgreSQL\18\bin\psql.exe" `
  -h 127.0.0.1 -p 5432 -U postgres -d postgres `
  -c "CREATE DATABASE omicsprism;"

## 如果数据库已经存在，PostgreSQL 会报错；可忽略，继续下一步。设置本地运行账号和迁移参数：
## 设置本地运行账号和迁移参数：
$env:OMICS_PRISM_MIGRATION_DATABASE_URL = `
  "postgresql://postgres:white0722@127.0.0.1:5432/omicsprism"

$env:OMICS_PRISM_APP_DB_PASSWORD = "local-app-password"

.\.venv\Scripts\python.exe scripts/migrate.py

## 2、建立 vLLM 隧道
ssh -N -L 18000:127.0.0.1:18000 wb@10.1.115.140

## 隧道保持运行后，在另一个本地终端执行：
Invoke-RestMethod http://127.0.0.1:18000/v1/models

.\.venv\Scripts\python.exe scripts/run_agent_eval_v2.py `
  --live-model `
  --base-url http://127.0.0.1:18000/v1 `
  --model Qwen3-14B-AWQ `
  --trials 1


## 3、启动三个本地后端进程
cd D:\JetBrains\PythonProjects\omicsprism-platform

$env:OMICS_PRISM_STORAGE_BACKEND = "postgres"
$env:OMICS_PRISM_RUNTIME_DATABASE_URL = `
  "postgresql://omics_app:local-app-password@127.0.0.1:5432/omicsprism"

$env:OMICS_PRISM_EXECUTOR = "redis"
$env:OMICS_PRISM_REDIS_URL = "redis://127.0.0.1:6379/0"
$env:OMICS_PRISM_FILE_STORAGE_BACKEND = "local"
$env:OMICS_PRISM_FILE_STORAGE_ROOT = `
  "D:\JetBrains\PythonProjects\omicsprism-platform\storage"
$env:OMICS_PRISM_RUNS_DIR = `
  "D:\JetBrains\PythonProjects\omicsprism-platform\runs"

$env:OMICS_PRISM_AGENT_MODEL_URL = "http://127.0.0.1:18000/v1"
$env:OMICS_PRISM_AGENT_MODEL_NAME = "Qwen3-14B-AWQ"
$env:OMICS_PRISM_AGENT_MODEL_API_KEY = ""
$env:OMICS_PRISM_AGENT_QUEUE = "omicsprism:agent-turns"


## 然后分别在三个终端运行：
API：
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload --port 8000

分析 Worker：
.\.venv\Scripts\python.exe -m backend.worker

Agent Runtime：
.\.venv\Scripts\python.exe -m backend.agent_runtime

## 4、启动前端
npm run dev --prefix frontend