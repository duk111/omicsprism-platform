# OmicsPrism 服务器部署模式（当前 v3）

> 文件名保留是为了兼容已有链接；本文已经更新为当前生产部署说明。
> 当前版本由云服务器、算力服务器和外部 vLLM 服务组成。

## 1. 部署结论

OmicsPrism 使用双服务器部署：

- 云服务器：用户入口、nginx、静态前端、API、PostgreSQL、Redis、MinIO 和 housekeeping。
- 算力服务器：Agent Runtime、分析 Worker 和 vLLM。
- 浏览器只访问云服务器 nginx，不直接访问 PostgreSQL、Redis、MinIO、Agent Runtime、分析 Worker 或 vLLM。
- API 将 Agent turn 写入 Redis 的 `omicsprism:agent-turns` 队列；算力服务器上的 Agent Runtime 消费该队列并执行 LangGraph。
- API 将分析 Job 写入 Redis 的 `omicsprism:jobs` 队列；算力服务器上的分析 Worker 消费该队列并执行 DEG、DEM、GMA 等分析。
- Agent Runtime 通过共享 PostgreSQL checkpointer 持久化图状态，并访问云服务器的 PostgreSQL、Redis 和 MinIO。
- Agent Runtime 通过本机 `127.0.0.1:18000` 访问算力服务器上的 vLLM。
- 不得启动已经废弃的旧版 `agent-worker`。

## 2. 当前请求链路

```text
浏览器
  |
  | https://<域名>/omicsprism/
  v
云服务器 nginx（容器名通常为 nginx，宿主机端口 8092）
  |-- /omicsprism/       -> 宿主机静态目录
  |-- /omicsprism/api/*  -> API 容器的 18086 端口
  v
云服务器 API（omicsprism-api-1）
  |-- PostgreSQL（omicsprism-postgres-1）
  |-- Redis（omicsprism-redis-1）
  |-- MinIO（omicsprism-minio-1）
  |-- omicsprism:agent-turns -> 算力服务器 omicsprism-agent-1
  |-- omicsprism:jobs        -> 算力服务器 omicsprism-worker-1

算力服务器
  |-- omicsprism-agent-1 -> LangGraph -> omicsprism-vllm:18000
  |-- omicsprism-worker-1 -> DEG/DEM/GMA
```

## 3. 服务和固定名称

### 3.1 云服务器

云服务器的 API 栈由仓库中的 `docker-compose.yml` 和
`docker-compose.expose.yml` 管理：

| 服务 | 容器名 | 宿主机端口 | 用途 |
| --- | --- | --- | --- |
| nginx | `nginx`（外部维护） | `8092` | 静态前端和 API 反向代理 |
| api | `omicsprism-api-1` | `18086 -> 8000` | FastAPI 和 Agent API |
| postgres | `omicsprism-postgres-1` | `15432 -> 5432` | 业务、Agent、Job 和 checkpoint 数据 |
| redis | `omicsprism-redis-1` | `16379 -> 6379` | Agent turn 和分析 Job 队列 |
| minio | `omicsprism-minio-1` | `19000 -> 9000` | 输入文件和分析结果产物 |
| housekeeping | `omicsprism-housekeeping-1` | 无 | 清理过期运行目录和对象 |

前端不是 Compose 服务。构建后的 `frontend/dist` 由宿主机 nginx 提供，当前目录为：

```text
/www/nginx/nginx_html/html/omicsprism/
```

### 3.2 算力服务器

| 服务 | 固定容器名 | 用途 |
| --- | --- | --- |
| Agent Runtime | `omicsprism-agent-1` | 消费 Agent turn，执行 LangGraph 和模型调用 |
| 分析 Worker | `omicsprism-worker-1` | 消费分析 Job，执行 DEG/DEM/GMA |
| vLLM | `omicsprism-vllm` | 提供 OpenAI-compatible 模型接口 |

`omicsprism-agent-1` 使用仓库中的 `docker-compose.agent-runtime.yml`，并使用
`network_mode: host`，所以容器内的 `127.0.0.1:18000` 指向算力服务器宿主机上的 vLLM。

`omicsprism-worker-1` 使用算力服务器本地维护的 `docker-compose.worker.yml`。
该文件可能不在 Git 仓库中，更新代码时必须保留服务器上的这份编排文件和 `.env.worker`。

vLLM 不由本仓库的 Compose 创建，不要因为平台代码更新而重建或删除它，除非明确要更新模型服务。

## 4. 端口和防火墙

| 端口 | 所在服务器 | 访问方 | 说明 |
| --- | --- | --- | --- |
| `8092` | 云服务器 | 浏览器 | nginx 对外入口 |
| `18086` | 云服务器 | nginx | API 宿主机端口，不给浏览器绕过 nginx 使用 |
| `15432` | 云服务器 | 算力服务器 | PostgreSQL 公网映射端口 |
| `16379` | 云服务器 | 算力服务器 | Redis 公网映射端口 |
| `19000` | 云服务器 | 算力服务器 | MinIO API 公网映射端口 |
| `18000` | 算力服务器 | Agent Runtime | vLLM，不对公网开放 |

生产防火墙至少应满足：

1. 只允许算力服务器固定 IP 访问云服务器的 `15432`、`16379`、`19000`。
2. `18000` 不允许公网访问，只允许本机 Agent Runtime 或受控内网客户端访问。
3. `18086` 只允许 nginx 或受控反向代理访问。
4. PostgreSQL、Redis、MinIO 不允许浏览器直接访问。

## 5. 环境变量边界

### 5.1 云服务器 `.env`

云服务器 API 和分析服务使用 PostgreSQL、Redis、MinIO 的容器网络地址：

```text
OMICS_PRISM_STORAGE_BACKEND=postgres
OMICS_PRISM_RUNTIME_DATABASE_URL=postgresql://omics_app:<password>@postgres:5432/omicsprism
OMICS_PRISM_EXECUTOR=redis
OMICS_PRISM_REDIS_URL=redis://redis:6379/0
OMICS_PRISM_REDIS_QUEUE=omicsprism:jobs
OMICS_PRISM_AGENT_QUEUE=omicsprism:agent-turns
OMICS_PRISM_FILE_STORAGE_BACKEND=s3
OMICS_PRISM_S3_ENDPOINT_URL=http://minio:9000
OMICS_PRISM_FILE_STORAGE_BUCKET=omicsprism
OMICS_PRISM_AGENT_MODEL_URL=http://<model-host>:<model-port>/v1
OMICS_PRISM_AGENT_MODEL_NAME=<served-model-name>
```

API 启动时仍要求 `OMICS_PRISM_AGENT_MODEL_URL` 和
`OMICS_PRISM_AGENT_MODEL_NAME` 非空，因为 API 会创建图和模型适配器；实际生产模型请求由算力服务器的 `omicsprism-agent-1` 执行。

### 5.2 算力服务器 `.env`

Agent Runtime 和分析 Worker 使用云服务器暴露的宿主机端口：

```text
OMICS_PRISM_STORAGE_BACKEND=postgres
OMICS_PRISM_RUNTIME_DATABASE_URL=postgresql://omics_app:<password>@<cloud-host>:15432/omicsprism
OMICS_PRISM_EXECUTOR=redis
OMICS_PRISM_REDIS_URL=redis://<cloud-host>:16379/0
OMICS_PRISM_REDIS_QUEUE=omicsprism:jobs
OMICS_PRISM_AGENT_QUEUE=omicsprism:agent-turns
OMICS_PRISM_FILE_STORAGE_BACKEND=s3
OMICS_PRISM_S3_ENDPOINT_URL=http://<cloud-host>:19000
OMICS_PRISM_S3_ACCESS_KEY_ID=<minio-user>
OMICS_PRISM_S3_SECRET_ACCESS_KEY=<minio-password>
OMICS_PRISM_FILE_STORAGE_BUCKET=omicsprism
```

Agent Runtime 还需要：

```text
OMICS_PRISM_AGENT_MODEL_URL=http://127.0.0.1:18000/v1
OMICS_PRISM_AGENT_MODEL_NAME=Qwen3-14B-AWQ
# 可选：不配置时，澄清参数解析子 Agent 复用上面的主 Agent 模型。
OMICS_PRISM_CLARIFICATION_MODEL_URL=
OMICS_PRISM_CLARIFICATION_MODEL_NAME=
OMICS_PRISM_CLARIFICATION_MODEL_API_KEY=
```

`.env`、`.env.worker`、模型访问密钥和数据库密码只保存在服务器，不提交 Git。

## 6. 云服务器更新顺序

```bash
cd /www/omicsprism-deploy/omicsprism-platform
git fetch origin
git switch master
git pull --ff-only origin master
```

先启动基础设施并保持远程 Worker 所需端口：

```bash
sudo docker compose \
  --env-file .env \
  -p omicsprism \
  -f docker-compose.yml \
  -f docker-compose.expose.yml \
  up -d postgres redis minio minio-init
```

执行迁移：

```bash
sudo docker compose \
  --env-file .env \
  -p omicsprism \
  -f docker-compose.yml \
  -f docker-compose.expose.yml \
  --profile migration run --rm --build migrate
```

重建 API 和 housekeeping。生产分析 Worker 不在云服务器重建：

```bash
sudo docker compose \
  --env-file .env \
  -p omicsprism \
  -f docker-compose.yml \
  -f docker-compose.expose.yml \
  up -d --build api housekeeping
```

前端在本地构建后上传到云服务器的 nginx 目录，不在服务器上重新安装前端环境：

```text
VITE_PUBLIC_BASE_PATH=/omicsprism/
VITE_API_BASE_PATH=/omicsprism/api
```

上传完成后，当前 nginx 容器名为 `nginx`：

```bash
sudo docker exec nginx nginx -t
sudo docker exec nginx nginx -s reload
```

如果容器名不确定，先执行：

```bash
sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' | grep -Ei 'nginx'
```

## 7. 算力服务器更新顺序

```bash
cd /data/wb/omicsprism-worker/omicsprism-platform
git fetch origin
git switch master
git pull --ff-only origin master
```

使用服务器本地的 Worker 编排重建分析 Worker：

```bash
sudo docker compose \
  --env-file .env.worker \
  -f docker-compose.worker.yml \
  up -d --build --force-recreate worker
```

使用仓库编排重建 Agent Runtime：

```bash
sudo docker compose \
  --env-file .env \
  -p omicsprism \
  -f docker-compose.agent-runtime.yml \
  up -d --build --force-recreate agent-runtime
```

确认容器：

```bash
sudo docker ps -a \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' \
  | grep -Ei 'omicsprism-(agent|worker|vllm)'
```

预期至少包括：

```text
omicsprism-agent-1
omicsprism-worker-1
omicsprism-vllm
```

## 8. nginx 路由契约

nginx 必须将 `/omicsprism/api/` 代理到云服务器 API，并关闭 SSE 缓冲：

```nginx
location ^~ /omicsprism/api/ {
    rewrite ^/omicsprism/api/(.*)$ /api/$1 break;
    proxy_pass http://<cloud-api-address>:18086;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 500M;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
}

location ^~ /omicsprism/ {
    alias /www/nginx/nginx_html/html/omicsprism/;
    try_files $uri $uri/ /omicsprism/index.html;
}
```

`<cloud-api-address>` 必须替换为 nginx 容器能够访问的云服务器地址；独立 nginx 容器内的 `127.0.0.1` 不等于宿主机地址。

`try_files` fallback 不能删除，否则 `/omicsprism/copilot` 等浏览器路由刷新会返回 404。

## 9. 发布后验证

云服务器：

```bash
sudo docker compose --env-file .env -p omicsprism \
  -f docker-compose.yml -f docker-compose.expose.yml ps
curl -fsS http://127.0.0.1:18086/health
sudo docker compose --env-file .env -p omicsprism exec -T redis \
  redis-cli LLEN omicsprism:agent-turns
sudo docker compose --env-file .env -p omicsprism exec -T redis \
  redis-cli LLEN omicsprism:jobs
```

算力服务器：

```bash
sudo docker logs --tail 100 omicsprism-agent-1
sudo docker logs --tail 100 omicsprism-worker-1
curl -fsS http://127.0.0.1:18000/v1/models
```

提交一次短 Agent 对话后，应确认：

1. API 返回 `202 Accepted`。
2. `omicsprism:agent-turns` 中出现并被 Agent Runtime 消费的 work item。
3. `omicsprism-agent-1` 出现 `agent.turn.processed` 或明确的 `agent.turn.failed`。
4. 对应 `agent_turns` 记录进入 completed、failed 或明确的 HITL 等待状态。
5. 普通分析请求进入 `omicsprism:jobs`，并由 `omicsprism-worker-1` 消费。

如果 Agent 队列增长，检查 Agent Runtime 到 PostgreSQL、Redis、MinIO 和 vLLM 的连接；如果分析队列增长，检查分析 Worker 和 Redis 的 `omicsprism:jobs` 配置。

## 10. 回滚边界

- 发布前记录 API、Agent Runtime、Worker 的 Git commit 和数据库迁移版本。
- 回滚时先回滚 API、Agent Runtime 和 Worker，再同步前端静态文件。
- 不删除 PostgreSQL、Redis、MinIO 数据卷。
- API、Agent Runtime、Worker 和前端必须来自兼容版本。
- 旧版 `agent-worker`、`nginx-all` 不属于当前部署名称，不要按旧文档重新创建。
