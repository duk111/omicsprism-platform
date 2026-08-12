# OmicsPrism 服务器代码更新流程

本文记录代码更新后如何同步到服务器。当前部署分为两台机器：

- 云服务器：对外提供网页、nginx、API、Postgres、Redis、MinIO、housekeeping。
- 算力服务器：运行 analysis worker、agent worker 与 vLLM；analysis worker 负责耗时分析，agent worker 负责 Copilot turn。

当前公网访问入口：

```text
http://111.170.173.174:8092/omicsprism/
```

常用路径：

```text
云服务器平台目录：/www/omicsprism-deploy/omicsprism-platform
云服务器分析库目录：/www/omicsprism-deploy/omicsprism
前端静态目录：/www/nginx/nginx_html/html/omicsprism

算力服务器平台目录：/data/wb/omicsprism-worker/omicsprism-platform
算力服务器分析库目录：/data/wb/omicsprism-worker/omicsprism
```

不要执行：

```bash
docker compose down -v
```

`-v` 会删除 volume，可能丢 Postgres/Redis/MinIO 数据。也不要在面板里单独重建 compose 管理的容器，容易造成容器还在但网络脱离 compose。

---

## 1. 判断本次改动属于哪一类

| 改动内容 | 需要更新的位置 |
| --- | --- |
| `frontend/` | 云服务器：重新 build 前端并复制 `dist` |
| `backend/` API 逻辑 | 云服务器：重建 `api` |
| `backend/` worker 逻辑 | 算力服务器：重建 `worker` |
| `backend/agent_worker.py`、`backend/app/agent/` | 算力服务器：重建 `agent-worker`；API 契约变更时云服务器也重建 `api` |
| `omicsprism/` 分析库、绘图、依赖 | 算力服务器：重建 `worker`；必要时云服务器也重建 `api` |
| `Dockerfile.backend`、`backend/requirements.txt`、`pyproject.toml` 依赖 | 云服务器和算力服务器都重新 build；算力服务器建议 `--no-cache` |
| `docker-compose.yml`、`docker-compose.expose.yml` | 云服务器按 compose 重建受影响服务 |
| `docker-compose.worker.yml`、`.env.worker` | 算力服务器重建 `worker` |
| `docker-compose.agent-worker.yml`、`.env.agent` | 算力服务器重建 `agent-worker` |
| 只改文档 | 不需要更新服务器 |

---

## 2. 更新前端

适用场景：修改 `frontend/`、交互页面、前端 API 路径、样式等。

在云服务器执行：

```bash
cd /www/omicsprism-deploy/omicsprism-platform
git pull

VITE_PUBLIC_BASE_PATH=/omicsprism/ VITE_API_BASE_PATH=/omicsprism/api npm run build --prefix frontend

rm -rf /www/nginx/nginx_html/html/omicsprism/*
cp -a frontend/dist/. /www/nginx/nginx_html/html/omicsprism/
```

验证前端路径：

```bash
curl -s http://127.0.0.1:8092/omicsprism/ | grep assets
grep -R "/api/wb" -n frontend/dist || true
```

预期：

- `index.html` 引用 `/omicsprism/assets/...`
- `frontend/dist` 里不再出现旧路径 `/api/wb`

浏览器使用强制刷新：

```text
Ctrl + F5
```

---

## 3. 更新云服务器 API

适用场景：修改 `backend/app/main.py`、API 路由、任务记录、文件服务、认证、Postgres 任务仓库等。

在云服务器执行：

```bash
cd /www/omicsprism-deploy/omicsprism-platform
git pull

docker rm -f omicsprism-api-1
docker compose -p omicsprism -f docker-compose.yml -f docker-compose.expose.yml up -d --build api
```

验证：

```bash
docker compose -p omicsprism -f docker-compose.yml -f docker-compose.expose.yml ps api
docker exec omicsprism-api-1 printenv OMICS_PRISM_STORAGE_BACKEND
docker exec omicsprism-api-1 printenv OMICS_PRISM_EXECUTOR
curl -i http://127.0.0.1:18086/health
docker exec nginx-all sh -c "curl -i http://172.17.0.1:18086/health"
```

预期：

```text
OMICS_PRISM_STORAGE_BACKEND=postgres
OMICS_PRISM_EXECUTOR=redis
health 返回 200
```

如果 nginx 容器访问 `172.17.0.1:18086` 失败，检查云服务器 `.env`：

```bash
grep -n "API_BIND_HOST\|API_PORT" .env
```

需要：

```bash
API_BIND_HOST=0.0.0.0
API_PORT=18086
```

---

## 4. 更新算力服务器 worker

适用场景：修改 `omicsprism/`、worker 逻辑、分析流程、绘图代码。

在算力服务器执行：

```bash
cd /data/wb/omicsprism-worker/omicsprism
git pull

cd /data/wb/omicsprism-worker/omicsprism-platform
git pull

sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker down
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker up -d --build

sudo docker compose \
  -p omicsprism-agent \
  -f docker-compose.agent-worker.yml \
  up -d --build --no-deps --force-recreate agent-worker
```

如果修改了依赖或 Dockerfile，使用无缓存重建：

```bash
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker down
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker build --no-cache worker
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker up -d
```

验证：

```bash
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker ps
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker logs --tail=120 worker
```

验证 worker 能连接云服务器三件套：

```bash
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker exec -T worker python - <<'PY'
import os, redis, psycopg, boto3

with psycopg.connect(os.environ["OMICS_PRISM_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
        cur.execute("select 1")
        print("Postgres:", cur.fetchone())

r = redis.Redis.from_url(os.environ["OMICS_PRISM_REDIS_URL"], decode_responses=True)
print("Redis:", r.ping(), "queue:", r.llen(os.environ.get("OMICS_PRISM_REDIS_QUEUE", "omicsprism:jobs")))

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["OMICS_PRISM_S3_ENDPOINT_URL"],
    aws_access_key_id=os.environ["OMICS_PRISM_S3_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["OMICS_PRISM_S3_SECRET_ACCESS_KEY"],
    region_name=os.environ.get("OMICS_PRISM_S3_REGION", "us-east-1"),
)
print("Buckets:", [b["Name"] for b in s3.list_buckets()["Buckets"]])
PY
```

预期：

```text
Postgres: (1,)
Redis: True
Buckets: ['omicsprism']
```

---

## 5. 更新算力服务器 agent worker

适用场景：修改 `backend/agent_worker.py`、`backend/app/agent/`、Copilot 模型适配或结果解读逻辑。

首次切换到仓库内 compose 时，确认 `.env.agent` 已存在；可参考 `deploy/agent-worker.env.example`，不要把真实密码提交到 Git：

```bash
cd /data/wb/omicsprism-worker/omicsprism-platform
test -f .env.agent
sudo chmod 600 .env.agent
```

更新并只重建 agent worker：

```bash
git pull

sudo docker compose \
  -p omicsprism-agent \
  -f docker-compose.agent-worker.yml \
  up -d --build --no-deps --force-recreate agent-worker
```

验证受版本控制的 compose、数据库连接和 vLLM：

```bash
sudo docker compose \
  -p omicsprism-agent \
  -f docker-compose.agent-worker.yml \
  config --services

sudo docker compose \
  -p omicsprism-agent \
  -f docker-compose.agent-worker.yml \
  ps

sudo docker exec omicsprism-agent-agent-worker-1 \
  python -c 'import os,psycopg; c=psycopg.connect(os.environ["OMICS_PRISM_RUNTIME_DATABASE_URL"],connect_timeout=10); print(c.execute("select current_user,current_database()").fetchone()); c.close()'

curl -fsS http://127.0.0.1:18000/v1/models | python3 -m json.tool

sudo docker logs \
  --since 2m \
  --timestamps \
  omicsprism-agent-agent-worker-1
```

预期服务名只有 `agent-worker`，数据库用户为 `omics_app`，vLLM 模型名与 `.env.agent` 一致。

回滚时只回滚 agent worker，不删除数据库表或审计记录：

```bash
git checkout <last-known-good-commit> -- \
  backend/agent_worker.py backend/app/agent docker-compose.agent-worker.yml

sudo docker compose \
  -p omicsprism-agent \
  -f docker-compose.agent-worker.yml \
  up -d --build --no-deps --force-recreate agent-worker
```

---

## 6. 更新系统依赖或 Python 依赖

适用场景：

- 修改 `Dockerfile.backend`
- 修改 `omicsprism/pyproject.toml`
- 修改 `omicsprism-platform/backend/requirements.txt`
- 新增 Kaleido/Chrome、PyDESeq2 等运行依赖

算力服务器必须无缓存重建 worker：

```bash
cd /data/wb/omicsprism-worker/omicsprism-platform
git pull

sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker down
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker build --no-cache worker
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker up -d
```

云服务器如 API 也依赖该变更：

```bash
cd /www/omicsprism-deploy/omicsprism-platform
git pull

docker rm -f omicsprism-api-1
docker compose -p omicsprism -f docker-compose.yml -f docker-compose.expose.yml up -d --build api
```

常见依赖验证：

```bash
# Kaleido 静态图导出需要 Chromium
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker exec -T worker chromium --version

# DEG 模块需要 PyDESeq2
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker exec -T worker python - <<'PY'
import pydeseq2
print("pydeseq2 ok", pydeseq2.__version__)
PY
```

---

## 7. 更新 housekeeping

适用场景：修改过期清理逻辑、MinIO 清理逻辑、任务 TTL 逻辑。

在云服务器执行：

```bash
cd /www/omicsprism-deploy/omicsprism-platform
git pull

docker rm -f omicsprism-housekeeping-1
docker compose -p omicsprism -f docker-compose.yml -f docker-compose.expose.yml up -d --build housekeeping
```

验证环境变量：

```bash
docker exec omicsprism-housekeeping-1 printenv | grep OMICS_PRISM
```

---

## 8. 更新后端口和网络检查

云服务器检查容器网络：

```bash
docker inspect omicsprism-api-1 --format '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}'
docker inspect omicsprism-postgres-1 --format '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}'
```

预期都包含：

```text
omicsprism_default
```

云服务器检查端口：

```bash
docker ps --format "table {{.Names}}\t{{.Ports}}" | grep -E "omicsprism-api|omicsprism-postgres|omicsprism-redis|omicsprism-minio"
```

当前约定：

```text
api:      0.0.0.0:18086 -> 8000
postgres: 0.0.0.0:15432 -> 5432
redis:    0.0.0.0:16379 -> 6379
minio:    0.0.0.0:19000 -> 9000
```

如果服务器实际使用了其他端口，以 `.env` 和 `.env.worker` 为准，两边必须一致。

---

## 9. 常见问题快速定位

### 9.1 网页报 502 Bad Gateway

含义：nginx 到 API 失败。

检查：

```bash
curl -i http://127.0.0.1:18086/health
docker exec nginx-all sh -c "curl -i http://172.17.0.1:18086/health"
docker compose -p omicsprism -f docker-compose.yml -f docker-compose.expose.yml logs --tail=200 api
```

常见原因：

- `API_BIND_HOST` 不是 `0.0.0.0`
- API 容器崩溃
- API 和 Postgres 不在同一个 Docker 网络
- nginx `/omicsprism/api/` proxy_pass 配置错误

### 9.2 网页一直显示 Waiting for worker

含义：任务已进入排队/等待阶段，但 worker 没正常接走或没更新状态。

云服务器：

```bash
docker exec omicsprism-redis-1 redis-cli llen omicsprism:jobs
```

算力服务器：

```bash
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker ps
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker logs --tail=200 worker
```

常见原因：

- worker `.env.worker` 仍连接旧 Postgres 端口
- 云服务器防火墙未放行算力服务器 IP
- worker 容器崩溃重启
- API 写入的 Postgres 和 worker 读取的 Postgres 不是同一个

### 9.3 上传时报 413 Request Entity Too Large

含义：nginx 上传大小限制。

检查 nginx 配置：

```bash
grep -n "omicsprism/api" -A20 -B5 /www/nginx/nginx_conf/conf.d/default.conf
```

`/omicsprism/api/` location 里应有：

```nginx
client_max_body_size 2G;
```

重载 nginx：

```bash
docker exec nginx-all nginx -t
docker exec nginx-all nginx -s reload
```

### 9.4 Kaleido requires Google Chrome

含义：worker 镜像里缺 Chromium。

处理：

```bash
cd /data/wb/omicsprism-worker/omicsprism-platform
git pull
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker build --no-cache worker
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker up -d
```

验证：

```bash
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker exec -T worker chromium --version
```

### 9.5 DEG module requires PyDESeq2

含义：worker 镜像没有安装 `omicsprism[deg]`。

处理同依赖更新：

```bash
cd /data/wb/omicsprism-worker/omicsprism-platform
git pull
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker build --no-cache worker
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker up -d
```

验证：

```bash
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker exec -T worker python - <<'PY'
import pydeseq2
print("pydeseq2 ok", pydeseq2.__version__)
PY
```

---

## 10. 最小完整更新清单

如果不确定改动属于哪类，但想保证线上完整更新，可执行：

云服务器：

```bash
cd /www/omicsprism-deploy/omicsprism-platform
git pull

VITE_PUBLIC_BASE_PATH=/omicsprism/ VITE_API_BASE_PATH=/omicsprism/api npm run build --prefix frontend
rm -rf /www/nginx/nginx_html/html/omicsprism/*
cp -a frontend/dist/. /www/nginx/nginx_html/html/omicsprism/

docker rm -f omicsprism-api-1
docker compose -p omicsprism -f docker-compose.yml -f docker-compose.expose.yml up -d --build api housekeeping
```

算力服务器：

```bash
cd /data/wb/omicsprism-worker/omicsprism
git pull

cd /data/wb/omicsprism-worker/omicsprism-platform
git pull

sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker down
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker up -d --build
```

如果依赖变更，把最后一条换成：

```bash
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker build --no-cache worker
sudo docker compose -f docker-compose.worker.yml -p omicsprism-worker up -d
```
