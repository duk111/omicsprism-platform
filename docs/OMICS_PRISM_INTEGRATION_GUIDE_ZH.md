# OmicsPrism 项目对接说明

> 文档版本：1.0
> 项目版本：0.5.0
> 更新日期：2026-07-21
> 适用对象：前端、后端、运维、算法和测试对接人员

## 1. 文档目的

本文用于帮助新接入的同事快速理解 OmicsPrism 的项目边界、运行架构、分析模块、页面路由、API、任务状态、存储方式和部署要求。

项目包含两个相互独立但配套使用的代码库：

- `omicsprism`：可单独发布和安装的 Python 分析软件包，包含 DEG、DEM、GMA 算法和可视化输出。
- `omicsprism-platform`：Web 平台，负责文件上传、参数收集、任务调度、进度展示、结果管理和下载。

平台不重新实现分析算法。所有科学计算逻辑应优先放在 `omicsprism`，平台只负责调用和展示。

## 2. 项目能力

| 模块 | 页面名称 | 后端 `analysis_type` | 主要用途 |
| --- | --- | --- | --- |
| DEG | Differentially Expressed Genes | `differential` | 根据 RNA-seq raw count 和样本元数据进行差异表达分析 |
| DEM | Differentially Expressed Metabolites | `dem` | 根据代谢物丰度矩阵和样本元数据筛选差异代谢物 |
| GMA | Gene-Metabolite Association | `correlation` | 联合转录组和代谢组，构建基因-代谢物关联及网络 |

主要用户流程如下：

```text
选择模块
  -> 上传 CSV 并填写参数
  -> 后端预检
  -> 创建任务
  -> Redis 入队
  -> Python Worker 执行分析
  -> PostgreSQL 保存任务状态
  -> MinIO/S3 保存上传文件和结果
  -> 前端通过 SSE 查看进度
  -> 浏览、下载结果
```

## 3. 技术栈

| 层级 | 技术 |
| --- | --- |
| 前端 | React 18、TypeScript、Vite、React Router、Plotly |
| API | Python、FastAPI、Uvicorn |
| 分析 Worker | Python、OmicsPrism、PyDESeq2、pandas、NumPy、scikit-learn、XGBoost 等 |
| 任务队列 | Redis |
| 业务数据 | PostgreSQL；本地开发可使用 JSON |
| 文件存储 | MinIO/S3；本地开发可使用本地目录 |
| 部署 | Docker Compose、Nginx/服务器面板静态站点 |

## 4. 代码目录

```text
PythonProjects/
├── omicsprism/
│   ├── src/omicsprism/
│   │   ├── deg/                 # DEG 分析
│   │   ├── dem/                 # DEM 分析
│   │   ├── core.py              # GMA 主流程编排
│   │   ├── selectors.py         # 候选筛选和建模
│   │   ├── modules.py           # 模块分析
│   │   └── visualization/       # 静态图和交互报告
│   ├── tests/
│   └── pyproject.toml
└── omicsprism-platform/
    ├── backend/
    │   ├── app/main.py          # FastAPI 路由
    │   ├── app/job_execution.py # 三类任务执行适配
    │   ├── app/job_store.py     # JSON/PostgreSQL 任务存储
    │   ├── app/file_service.py  # 本地/S3 文件处理
    │   ├── app/preflight.py     # 上传数据预检
    │   ├── app/settings.py      # 环境变量
    │   └── worker.py            # Redis Worker 入口
    ├── frontend/
    │   ├── src/App.tsx          # 页面路由和业务界面
    │   ├── src/api.ts           # API 和公共路径适配
    │   └── src/interactive/     # 独立交互图页面
    ├── docker-compose.yml
    └── Dockerfile.backend
```

## 5. 当前部署拓扑

当前生产模式采用独立计算 Worker。推荐理解为两类服务器：

```text
用户浏览器
    |
    v
Nginx / 静态前端
    |
    v
FastAPI API ---------------------- PostgreSQL
    |                                  |
    +------------ Redis ---------------+
                     |
                     v
             算力服务器 Worker
                     |
                     v
                 OmicsPrism
                     |
                     v
                  MinIO/S3
```

API 和 Worker 必须连接同一套 PostgreSQL、Redis 队列和 MinIO/S3：

- Redis URL 和队列名称必须完全一致。
- PostgreSQL 数据库必须共享，否则 Worker 更新的进度不会出现在 API。
- MinIO/S3 endpoint、bucket、prefix 和凭据必须一致，否则 Worker 无法读取输入或回传结果。
- Worker 镜像或 Python 环境必须安装与 API 兼容的 `omicsprism` 版本。

Docker Compose 默认也包含一个 `worker` 服务。生产环境若在独立算力服务器运行 Worker，应避免同一队列上启动不需要的额外 Worker，或明确使用 Worker 并发和队列隔离策略。

## 6. 访问地址和前端路由

当前公开入口：

```text
http://111.170.173.174:8092/omicsprism/
```

不要将 `/omicsprism/index.html` 作为对外入口。前端使用 Browser Router，主要路由如下：

| URL | 页面 |
| --- | --- |
| `/omicsprism/` | Landing Page |
| `/omicsprism/home` | 分析模块主页 |
| `/omicsprism/new` | 与 Home 相同的模块选择页 |
| `/omicsprism/deg` | DEG 表单 |
| `/omicsprism/dem` | DEM 表单 |
| `/omicsprism/gma` | GMA 表单 |
| `/omicsprism/jobs` | 当前浏览器会话的任务列表 |
| `/omicsprism/jobs/:jobId` | 任务进度和日志 |
| `/omicsprism/jobs/:jobId/results` | 任务结果 |
| `/omicsprism/download` | 示例数据和本地包入口 |
| `/omicsprism/help/tutorial` | Tutorial |
| `/omicsprism/help/contact` | Contact |
| `/omicsprism/interactive/:jobId/:pageId` | 独立交互图页面 |

Nginx 必须将前端深层路由回退到 `/omicsprism/index.html`，否则用户刷新 `/jobs/:jobId` 等页面时会出现 404。

## 7. API 基础约定

浏览器生产环境访问前缀：

```text
/omicsprism/api
```

FastAPI 服务内部仍使用：

```text
/api
```

Nginx 负责将 `/omicsprism/api/*` 重写并代理到 API 容器。前端生产构建参数为：

```bash
VITE_PUBLIC_BASE_PATH=/omicsprism/
VITE_API_BASE_PATH=/omicsprism/api
```

API 响应会返回 `X-Request-ID`。排查线上错误时，应同时记录：

- `jobId`
- `X-Request-ID`
- 请求时间
- 分析类型
- Worker 日志中的对应任务记录

## 8. 会话和数据隔离

当前平台使用匿名浏览器会话识别任务所有者，Cookie 名称为：

```text
omicsprism_session
```

Cookie 属性当前为：

- `HttpOnly`
- `SameSite=Lax`
- 有效期 30 天
- Path 为 `/`
- 当前代码中 `Secure=false`

对接时需要注意：

1. 前端请求必须携带 Cookie，现有 `apiFetch` 已设置 `credentials: include`。
2. 第三方调用 API 时要持续保存并发送同一个 Cookie。
3. Job 不属于当前 Cookie 会话时，接口故意返回 404，而不是 403。
4. SSE 进度连接同样依赖 Cookie。
5. 若未来启用 HTTPS，应将生产 Cookie 调整为 `Secure=true`。

这套机制不是完整账号系统。如需统一登录、跨设备查看任务或组织权限，需要另行接入正式认证体系，并迁移 Job owner 逻辑。

## 9. 核心 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | API 健康检查 |
| POST | `/api/jobs/preflight` | 上传文件和参数预检，不创建任务 |
| POST | `/api/jobs` | 创建并入队任务 |
| GET | `/api/jobs` | 查询当前会话任务 |
| GET | `/api/jobs/{jobId}` | 查询任务详情 |
| GET | `/api/jobs/{jobId}/progress` | 查询一次进度 |
| GET | `/api/jobs/{jobId}/progress/events` | SSE 实时进度 |
| GET | `/api/jobs/{jobId}/logs` | 获取任务日志 |
| GET | `/api/jobs/{jobId}/files` | 获取输入、结果文件和报告链接 |
| GET | `/api/jobs/{jobId}/images` | 获取结果图列表 |
| GET | `/api/jobs/{jobId}/figure-data/{figureId}` | 获取交互图数据 |
| GET | `/api/jobs/{jobId}/download/{path}` | 下载任务文件 |
| GET | `/api/jobs/{jobId}/reports/summary` | 打开汇总报告 |
| GET | `/api/jobs/{jobId}/reports/interactive` | 打开交互报告 |
| POST | `/api/jobs/{jobId}/cancel` | 取消排队或运行中任务 |
| DELETE | `/api/jobs/{jobId}` | 清理上传文件和结果，并软删除任务记录 |

## 10. 创建任务的数据契约

`POST /api/jobs/preflight` 和 `POST /api/jobs` 均使用 `multipart/form-data`。

### 10.1 DEG

```text
analysis_type=differential
counts=<RNA-seq raw count CSV>
metadata=<sample metadata CSV>
compare_field=treatment
tested_levels=salt
reference_level=control
same_fields=line,timepoint
padj_cutoff=0.05
log2fc_cutoff=1.0
min_total_count=10
min_replicates=2
```

必填文件为 `counts` 和 `metadata`。`compare_field`、`tested_levels` 和 `reference_level` 必填。

### 10.2 DEM

```text
analysis_type=dem
metabs=<metabolite abundance CSV>
metadata=<sample metadata CSV>
compare_field=treatment
tested_levels=salt
reference_level=control
same_fields=line,timepoint
padj_cutoff=0.05
log2fc_cutoff=1.0
vip_cutoff=1.0
max_missing_fraction=0.5
impute_method=half-min
normalize=true
log_transform=true
min_replicates=2
n_orthogonal_components=1
```

必填文件为 `metabs` 和 `metadata`。

### 10.3 GMA

```text
analysis_type=correlation
transcriptome=<transcriptome matrix CSV>
metabolome=<metabolome matrix CSV>
group=<group table CSV>
trans_log2=true
metab_log2=true
```

三个文件都必须提供。当前前端中转录组和代谢组的 Log2 选项默认开启。

### 10.4 Curl 示例

预检和创建任务应复用同一个 Cookie 文件：

```bash
curl -c cookies.txt -b cookies.txt \
  -F "analysis_type=differential" \
  -F "counts=@raw_count.csv" \
  -F "metadata=@metadata.csv" \
  -F "compare_field=treatment" \
  -F "tested_levels=salt" \
  -F "reference_level=control" \
  -F "same_fields=line,timepoint" \
  http://127.0.0.1:18086/api/jobs/preflight
```

预检返回 `can_submit=true` 后，再向 `/api/jobs` 发送相同表单。

## 11. 输入数据要求

通用要求：

- 文件使用 CSV，样本 ID 在所有输入文件中必须一致。
- 特征矩阵为 features × samples，即行是基因/代谢物，列是样本。
- 第一列是特征 ID。
- 不允许重复特征 ID 或重复样本列。
- Metadata/Group 中的样本必须能与矩阵列名匹配。

GMA group 表要求至少包含：

```text
sample_id,group1,group2
```

DEG/DEM 对比设计注意事项：

- `same_fields` 只填写要求相同的阻断变量，例如 `line,timepoint`。
- `same_fields` 不能包含 `compare_field`。
- 每个有效对比必须同时包含 tested level 和 reference level。
- 样本不足会导致无法生成有效 contrast。

示例数据可从 `/omicsprism/download` 下载。

## 12. 任务状态和进度

状态枚举：

```text
queued -> running -> succeeded
                  -> failed
queued/running    -> cancelled
```

前端优先连接 SSE：

```text
GET /api/jobs/{jobId}/progress/events
```

服务端只在进度内容变化时发送 `progress` 事件，结束后发送 `complete` 事件。Nginx 对该接口必须：

- 关闭代理缓冲。
- 设置足够长的 `proxy_read_timeout`。
- 不缓存 SSE 响应。

前端 SSE 连接异常时会回退为轮询模式。

## 13. 结果展示约定

- DEG 和 DEM 结果页按图类型分组，使用默认关闭的折叠区域。
- GMA 保持平铺式结果展示。
- 当前 Web 结果页只展示 SVG 结果图。
- CSV 和 ZIP 结果通过结果文件列表下载。
- 删除任务时，平台会清理本地或对象存储中的上传文件和结果文件，并将任务标记为已删除。

分析产物通常包括：

- 结果 CSV 表
- SVG/PNG 静态图
- `OmicsPrism_results.zip`
- HTML 汇总报告或交互报告
- `omicsprism.log`
- 交互图使用的 JSON 数据

## 14. 关键环境变量

### 数据库和任务队列

```text
OMICS_PRISM_STORAGE_BACKEND=postgres
OMICS_PRISM_RUNTIME_DATABASE_URL=postgresql://omics_app:...
OMICS_PRISM_EXECUTOR=redis
OMICS_PRISM_REDIS_URL=redis://...
OMICS_PRISM_REDIS_QUEUE=omicsprism:jobs
```

### 文件存储

```text
OMICS_PRISM_FILE_STORAGE_BACKEND=s3
OMICS_PRISM_S3_ENDPOINT_URL=http://minio:9000
OMICS_PRISM_S3_REGION=us-east-1
OMICS_PRISM_S3_ACCESS_KEY_ID=...
OMICS_PRISM_S3_SECRET_ACCESS_KEY=...
OMICS_PRISM_FILE_STORAGE_BUCKET=omicsprism
OMICS_PRISM_FILE_STORAGE_PREFIX=jobs
```

### 配额和清理

```text
OMICS_PRISM_MAX_CONCURRENT_JOBS_PER_USER=2
OMICS_PRISM_MAX_CONCURRENT_JOBS_PER_PROJECT=1
OMICS_PRISM_JOB_HISTORY_TTL_HOURS=24
OMICS_PRISM_HOUSEKEEPING_INTERVAL_SECONDS=3600
OMICS_PRISM_FILE_STORAGE_JOB_TTL_DAYS=30
OMICS_PRISM_FILE_STORAGE_FAILED_JOB_TTL_DAYS=14
```

### Web 和日志

```text
OMICS_PRISM_CORS_ORIGINS=http://localhost:5173
OMICS_PRISM_LOG_LEVEL=INFO
```

生产环境不得沿用 `.env.example` 中的默认密码。API、Worker 和 housekeeping 使用的存储凭据必须同步更新。

## 15. 本地开发

在两个仓库的共同父目录中执行：

```powershell
cd omicsprism
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[deg]"

cd ..\omicsprism-platform
python -m pip install -r backend\requirements.txt
npm install --prefix frontend
```

本地开发建议：

```text
OMICS_PRISM_STORAGE_BACKEND=json
OMICS_PRISM_EXECUTOR=local
OMICS_PRISM_FILE_STORAGE_BACKEND=local
```

启动 API：

```powershell
python -m uvicorn backend.app.main:app --reload --port 8000
```

启动前端：

```powershell
npm run dev --prefix frontend
```

开发地址为 `http://localhost:5173`，Vite 将 `/api` 代理到 `127.0.0.1:8000`。

## 16. 生产构建和 Nginx

构建前端：

```bash
VITE_PUBLIC_BASE_PATH=/omicsprism/ \
VITE_API_BASE_PATH=/omicsprism/api \
npm run build --prefix frontend
```

关键 Nginx 配置：

```nginx
location ^~ /omicsprism/api/ {
    rewrite ^/omicsprism/api/(.*)$ /api/$1 break;
    proxy_pass http://127.0.0.1:18086;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 500M;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
}

location = /omicsprism {
    return 301 /omicsprism/;
}

location ^~ /omicsprism/ {
    alias /www/nginx/nginx_html/html/omicsprism/;
    try_files $uri $uri/ /omicsprism/index.html;
}
```

重新部署前端后应检查：

```text
/omicsprism/
/omicsprism/home
/omicsprism/jobs
/omicsprism/help/tutorial
```

每个地址都要能直接打开并正常刷新。

## 17. 对接职责建议

### 算法侧

- 在 `omicsprism` 中维护分析实现、参数含义和结果文件格式。
- 修改输出文件名或目录结构时，同步通知平台侧。
- 保证 CLI/API 调用和 Web Worker 调用结果一致。

### 平台后端

- 维护上传字段、参数映射、任务状态和错误结构。
- 保证 API 与 Worker 使用一致的软件包版本和环境变量。
- 维护 PostgreSQL、Redis 和对象存储的生命周期。

### 前端

- 使用后端枚举值，不直接依赖中文或英文展示文案判断业务。
- 所有 API 调用携带 Cookie。
- 新页面必须增加 Browser Router 路由，并确认 Nginx 深层路由可刷新。
- 结果文件和图片地址统一经过 `apiUrl`/`assetUrl` 处理。

### 运维

- 管理 Nginx、TLS、API 端口、防火墙和存储凭据。
- 确认算力服务器能访问 PostgreSQL、Redis 和 MinIO/S3。
- 监控 API、Worker、Redis、数据库、对象存储和磁盘容量。
- 更新服务时先确认队列中是否存在运行中任务。

## 18. 联调验收清单

- [ ] `/health` 返回 `{"status":"ok"}`。
- [ ] `/omicsprism/` 能打开 Landing Page。
- [ ] 所有前端路由可以直接刷新，不返回 404。
- [ ] DEG 示例数据可以通过预检、创建任务并完成。
- [ ] DEM 示例数据可以通过预检、创建任务并完成。
- [ ] GMA 示例数据可以通过预检、创建任务并完成。
- [ ] 创建任务后 URL 变为 `/jobs/:jobId`。
- [ ] SSE 能实时更新进度；断开后可回退轮询。
- [ ] 完成后进入 `/jobs/:jobId/results`。
- [ ] DEG/DEM SVG 图和结果表可以展开、查看、下载。
- [ ] GMA 结果和交互报告可以打开。
- [ ] 浏览器前进和后退正常。
- [ ] 更换浏览器或清除 Cookie 后不能访问旧会话任务。
- [ ] 删除任务后上传文件和结果文件被清理。
- [ ] Worker 重启后新任务仍能消费。
- [ ] Nginx 上传大小和超时支持真实数据规模。

## 19. 常见问题

### 页面刷新后 404

检查 Nginx 的 `try_files` 是否回退到 `/omicsprism/index.html`，并确认前端使用 `/omicsprism/` 作为构建 base。

### My Jobs 为空或 Job 返回 404

先检查浏览器是否携带原来的 `omicsprism_session` Cookie。当前任务按匿名会话隔离。

### API 创建任务成功但 Worker 不执行

检查 API 和 Worker 的 Redis URL、队列名称是否一致，再查看 Worker 是否正在运行。

### Worker 找不到输入文件

检查 API 和 Worker 是否连接同一个 MinIO/S3 bucket 和 prefix，确认对象存储凭据与网络连通性。

### 前端一直显示 Connecting 或 Polling

检查 `/progress/events` 是否被 Nginx 缓冲或提前断开，并确认 Cookie 能传递到 SSE 请求。

### DEG/DEM 报错 `No valid contrasts were generated`

检查 `same_fields`、`compare_field`、tested/reference level 以及每组重复数。`same_fields` 不应包含对比字段。

## 20. 当前需要关注的事项

- 当前会话机制适合轻量匿名使用，不等同于正式账号权限系统。
- Cookie 当前未启用 `Secure`，上线 HTTPS 后应调整。
- 前端 Plotly bundle 较大，构建会出现 chunk size 警告，后续可按图表页面做按需加载。
- 生产环境的 Worker 独立部署时，需要建立明确的版本发布和回滚流程。
- 对分析参数、输入契约或输出目录的修改，应同时更新前端表单、后端参数映射、示例数据和本文档。

## 21. 交接时建议提供的信息

以下信息不应写入 Git，但应通过安全渠道交付给实际运维或对接人员：

- 服务器登录方式和部署目录
- Nginx 配置文件位置
- PostgreSQL 地址和账号
- Redis 地址、密码和队列名
- MinIO/S3 endpoint、bucket 和凭据
- 算力服务器地址和 Worker 启停方式
- 当前发布版本、镜像标签和回滚版本
- 日志目录、监控入口和告警联系人
