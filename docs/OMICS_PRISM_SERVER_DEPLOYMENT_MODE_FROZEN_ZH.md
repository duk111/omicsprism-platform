# OmicsPrism 服务器部署模式（冻结版）

> 创建日期：2026-08-25  
> 状态：冻结。本文档创建后不得修改。部署模式发生变化时，必须新建带日期或版本号的文档，不得回写本文档。

## 1. 部署结论

OmicsPrism 采用“双服务器 + 云服务器统一入口”的部署模式：

- **云服务器**负责对外访问入口、前端静态文件、API、PostgreSQL、Redis 和 MinIO。
- **算力服务器**负责原有组学分析 worker，并提供 vLLM 模型服务。
- 用户浏览器只访问云服务器 nginx，不直接访问算力服务器、数据库、Redis、MinIO 或 vLLM。
- API 通过 Redis 写入分析任务；算力服务器上的 analysis worker 从 Redis 取任务并执行分析。
- 当前 v3 Agent graph 在 API 进程内编排，不再使用旧版独立 `agent-worker` 控制面。API 通过 `OMICS_PRISM_AGENT_MODEL_URL` 访问 vLLM；vLLM 可以部署在算力服务器，也可以部署在其他受控模型服务器。

## 2. 实际请求链路

```text
浏览器
  │
  │ http://<云服务器>:8092/omicsprism/
  ▼
云服务器 nginx 容器
  ├─ 返回 /omicsprism/ 下的 frontend/dist 静态文件
  └─ /omicsprism/api/*
       │
       ▼
云服务器 API 容器（主机 18086 -> 容器 8000）
  ├─ Cookie 会话、普通分析 API、Copilot API/SSE
  ├─ PostgreSQL（主机 15432 -> 容器 5432）
  ├─ Redis（主机 16379 -> 容器 6379）
  ├─ MinIO（主机 19000 -> 容器 9000）
  └─ OMICS_PRISM_AGENT_MODEL_URL -> vLLM
                                      │
                                      ▼
算力服务器 vLLM（通常 127.0.0.1:18000 或受控内网地址）

云服务器 Redis
  ▲
  │ 公网/专用网络连接
  │
算力服务器 analysis worker
  ├─ 读取 Redis 中的 job id
  ├─ 通过 PostgreSQL 读取任务和状态
  ├─ 执行 OmicsPrism 分析算法
  └─ 通过 MinIO 写入输入/结果对象
```

## 3. 两台服务器的职责

### 3.1 云服务器

云服务器是唯一的用户入口，运行以下服务：

| 服务 | 作用 | 典型端口 |
| --- | --- | --- |
| nginx 容器 | 静态前端、API 反向代理 | `8092` |
| `api` 容器 | FastAPI、普通业务、Copilot graph | 主机 `18086` -> 容器 `8000` |
| `postgres` 容器 | 业务数据、任务数据、Agent 数据 | 主机 `15432` -> 容器 `5432` |
| `redis` 容器 | 分析 job 队列 | 主机 `16379` -> 容器 `6379` |
| `minio` 容器 | 输入文件和结果文件 | 主机 `19000` -> 容器 `9000` |
| `housekeeping` 容器 | 清理过期运行目录和对象 | 无公网端口 |

前端构建产物不是独立 Docker 服务。它由 nginx 从宿主机目录提供，例如：

```text
/www/nginx/nginx_html/html/omicsprism/
```

### 3.2 算力服务器

算力服务器不提供用户网页入口，运行以下服务：

| 服务 | 作用 | 连接方向 |
| --- | --- | --- |
| analysis worker | 执行 DEG、DEM、GMA 等分析 | 连接云服务器 `15432/16379/19000` |
| vLLM | 提供 Agent 使用的本地大模型 | 由 API 通过模型 URL 访问 |

当前仓库的 v3 版本没有受版本控制的独立 `docker-compose.agent-worker.yml`。不要根据旧部署记录重新启动旧版 `agent-worker`；Agent graph 的入口是 API 启动时的 `create_agent_api_context()`。

## 4. 端口和防火墙

| 端口 | 所在服务器 | 允许的访问者 | 说明 |
| --- | --- | --- | --- |
| `8092` | 云服务器 | 用户浏览器 | 对外网页入口 |
| `18086` | 云服务器 | nginx 容器/云服务器反向代理 | API 入口，不给浏览器绕过 nginx 使用 |
| `15432` | 云服务器 | 算力服务器 analysis worker | PostgreSQL 公网映射端口 |
| `16379` | 云服务器 | 算力服务器 analysis worker | Redis 公网映射端口 |
| `19000` | 云服务器 | 算力服务器 analysis worker | MinIO API 公网映射端口 |
| `18000` | 算力服务器 | API 所在服务器或受控内网客户端 | vLLM；禁止浏览器直接访问 |

生产防火墙至少应满足：

1. 仅允许算力服务器固定出口 IP 访问云服务器 `15432/16379/19000`。
2. `18000` 不开放给公网；若 API 在云服务器，允许云服务器到 vLLM 的单向访问。
3. `18086` 仅允许 nginx 或受控反向代理访问；是否绑定 `127.0.0.1` 或 `0.0.0.0` 取决于 nginx 与 API 的网络位置。
4. PostgreSQL、Redis、MinIO 不允许用户浏览器直接访问。

## 5. 配置边界

### 5.1 云服务器 API 配置

API 使用运行时数据库用户，不使用迁移管理员账号：

```text
OMICS_PRISM_STORAGE_BACKEND=postgres
OMICS_PRISM_RUNTIME_DATABASE_URL=postgresql://omics_app:<password>@<cloud-host>:15432/omicsprism
OMICS_PRISM_EXECUTOR=redis
OMICS_PRISM_REDIS_URL=redis://<cloud-host>:16379/0
OMICS_PRISM_REDIS_QUEUE=omicsprism:jobs
OMICS_PRISM_FILE_STORAGE_BACKEND=s3
OMICS_PRISM_S3_ENDPOINT_URL=http://<cloud-host>:19000
OMICS_PRISM_FILE_STORAGE_BUCKET=omicsprism
OMICS_PRISM_AGENT_MODEL_URL=http://<model-host>:<model-port>/v1
OMICS_PRISM_AGENT_MODEL_NAME=<served-model-name>
```

`OMICS_PRISM_AGENT_MODEL_URL` 必须与实际模型适配器的 OpenAI-compatible API 基础路径一致。若 vLLM 提供 `/v1/chat/completions`，配置其 `/v1` 基础地址；不要把不支持的 `/responses` 路径填入 vLLM 配置。

API compose 固定为单 Uvicorn worker，因为当前 graph 使用进程内 checkpointer：

```text
--workers 1
```

不得为了提高并发而直接改成多进程；如需改变 checkpointer 或进程模型，必须另行设计、验证并新建部署文档。

### 5.2 算力服务器 analysis worker 配置

analysis worker 只需要云服务器数据服务地址：

```text
OMICS_PRISM_STORAGE_BACKEND=postgres
OMICS_PRISM_RUNTIME_DATABASE_URL=postgresql://omics_app:<password>@<cloud-host>:15432/omicsprism
OMICS_PRISM_EXECUTOR=redis
OMICS_PRISM_REDIS_URL=redis://<cloud-host>:16379/0
OMICS_PRISM_REDIS_QUEUE=omicsprism:jobs
OMICS_PRISM_FILE_STORAGE_BACKEND=s3
OMICS_PRISM_S3_ENDPOINT_URL=http://<cloud-host>:19000
```

worker 不需要接收迁移管理员密码，也不需要接收 vLLM 凭据。

### 5.3 密钥规则

- `.env`、worker 环境文件和 vLLM 访问密钥只保存在服务器，不提交 Git。
- 仓库只提交 `.env.example` 等无密钥模板。
- 数据库迁移使用管理员 DSN；API 与 worker 使用 `omics_app` 最小权限账号。
- MinIO bucket 保持私有，文件下载经过已认证 API。

## 6. 云服务器部署顺序

以下顺序适用于首次部署或重建云服务器：

```bash
cd /www/omicsprism-deploy/omicsprism-platform

# 1. 准备服务器私有 .env
cp .env.example .env

# 2. 启动基础设施，并为远程 worker 发布数据端口
sudo docker compose -p omicsprism \
  -f docker-compose.yml \
  -f docker-compose.expose.yml \
  up -d postgres redis minio minio-init

# 3. 执行一次数据库迁移
sudo docker compose -p omicsprism \
  --profile migration run --rm migrate

# 4. 启动 API、housekeeping
sudo docker compose -p omicsprism \
  -f docker-compose.yml \
  -f docker-compose.expose.yml \
  up -d --build api housekeeping

# 5. 构建前端并同步到 nginx 宿主机目录
npm ci --prefix frontend
npm run build --prefix frontend
sudo rsync -a --delete frontend/dist/ \
  /www/nginx/nginx_html/html/omicsprism/

# 6. 检查并重载 nginx
sudo docker exec nginx-all nginx -t
sudo docker exec nginx-all nginx -s reload
```

如果 API 与 nginx 在同一 Docker 网络或同一宿主机，`API_BIND_HOST` 应按实际网络设置；nginx 必须能访问 API 的 `18086`。重建时必须同时使用 `docker-compose.expose.yml`，否则远程 analysis worker 可能看不到 `15432/16379/19000`。

## 7. 算力服务器部署顺序

```bash
cd /data/wb/omicsprism-worker/omicsprism-platform

# 1. 获取与云服务器兼容的同一版本代码
git pull

# 2. 重建 analysis worker（使用算力服务器实际维护的 worker compose）
sudo docker compose -f <analysis-worker-compose.yml> \
  up -d --build worker

# 3. 确认 vLLM 已加载目标模型
curl -fsS http://127.0.0.1:18000/health
curl -fsS http://127.0.0.1:18000/v1/models | python3 -m json.tool
```

当前仓库不包含 `<analysis-worker-compose.yml>` 的固定文件名；交接时必须使用算力服务器现有、经过人工确认的 analysis worker 编排，不得凭空创建第二套 compose。worker 容器的镜像构建上下文必须包含 `omicsprism` 分析库和 `omicsprism-platform` 后端。

## 8. nginx 入口契约

云服务器 nginx 必须保持以下语义：

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

`try_files` fallback 不能删除，否则直接访问 `/omicsprism/copilot` 或刷新浏览器路由会返回 404。API/SSE 代理不能启用会截断长请求的 buffering 或过短 read timeout。

## 9. 发布后验收

云服务器：

```bash
curl -fsS http://127.0.0.1:18086/health
curl -fsS http://<public-host>:8092/omicsprism/health
sudo docker compose -p omicsprism ps
```

算力服务器：

```bash
curl -fsS http://127.0.0.1:18000/health
curl -fsS http://127.0.0.1:18000/v1/models | python3 -m json.tool
sudo docker ps
```

功能验收至少包括：

1. 浏览器访问 `/omicsprism/` 并能加载静态资源。
2. `/omicsprism/copilot` 能创建会话并发送普通对话。
3. 上传数据后，Agent 使用真实 DatasetProfile、metadata 字段和值进行分析请求理解。
4. 普通手工分析仍能创建 job，analysis worker 能从 Redis 取出并完成任务。
5. 结果页、进度页和文件下载可用。
6. vLLM 不可用时，原手工分析、任务查询、结果查看和下载仍可用；只有 Copilot 能力降级。

## 10. 故障定位顺序

### 页面 200，但 API 请求失败

依次检查 nginx 配置、API 容器和 API 端口：

```bash
sudo docker exec nginx-all nginx -t
sudo docker compose -p omicsprism ps api
curl -i http://127.0.0.1:18086/health
```

### 任务一直排队

任务排队属于原有 analysis 链路，先检查 Redis 和 analysis worker，不要只看 Agent 日志：

```bash
sudo docker exec omicsprism-redis-1 redis-cli LLEN omicsprism:jobs
sudo docker logs --since 10m <analysis-worker-container>
```

确认 API 与 analysis worker 使用相同的 Redis 地址、数据库编号和队列名。

### Copilot 返回模型错误或 404

确认 API 的 `OMICS_PRISM_AGENT_MODEL_URL` 与模型服务真实路由匹配：

```bash
curl -fsS <model-host>/health
curl -fsS <model-host>/v1/models
```

vLLM OpenAI-compatible 服务通常使用 `/v1/chat/completions`。如果日志出现请求 `/responses` 的 404，说明当前模型适配器或模型 URL 指向了不兼容的 API 路径；这不是数据库或 Redis 故障。

### 算力服务器无法访问云服务器数据服务

检查云服务器是否使用 expose compose 启动，并从算力服务器测试：

```bash
nc -vz <cloud-host> 15432
nc -vz <cloud-host> 16379
nc -vz <cloud-host> 19000
```

若端口可达但连接被关闭，检查 PostgreSQL/Redis/MinIO 容器状态、端口映射、账号权限和云服务器防火墙白名单。

## 11. 版本与回滚边界

- 云服务器 API、算力服务器 analysis worker 和前端静态文件必须来自兼容版本。
- 发布前记录 Git commit、镜像 tag、环境文件版本和数据库迁移编号。
- 回滚代码时不删除 PostgreSQL、Redis、MinIO 数据卷。
- 先回滚 API/worker，再同步前端静态文件，确保前端契约不领先于后端。
- 任何新的服务器拓扑、进程模型、模型协议或端口变更，都新建部署文档；不得修改本冻结版。
