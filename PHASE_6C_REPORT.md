# Phase 6 Gate C 报告：HTTP/SSE 契约

> 状态：**已完成并关闭**。2026-08-03，服务器 PostgreSQL 验证、完整回归、前端生产构建和人工红线审阅均通过；契约已切换到 Gate D。
> 基线：Phase 6 Gate A/B 已完成并通过人工审阅；本 Gate 未新增 migration、业务工具、模型能力或前端 `/copilot` 页面。

## 1. 本 Gate 做了什么

- 新增 `backend/app/agent/api.py`，提供 `/api/agent/*` thread、message、turn、input bundle、approval 与 SSE 路由。
- 新增 `backend/app/agent/bootstrap.py`，API context 只装配 product/state/plan/approval/job/file store，不含 `VllmModelAdapter`、协调器或工具执行器。
- `main.py` 只 include agent router；非 PostgreSQL 装配下 agent 路由返回 503，原 `/health` 与 `/api/jobs/*` 不受影响。
- turn 请求在 repository 的同一事务中写入 user message 与 queued turn，返回 202；同 key 重放返回原 turn，不同 request hash 或同 thread 第二个活动 turn 返回 409。
- `focus_job_ids` 在入队前逐个做 owner 校验；PostgreSQL 装配在同一事务中更新 run focus，worker claim 前即可读取。
- multipart 暂存只接受 6 个以内、受控角色的 CSV；单文件 50 MB、bundle 150 MB，失败时清理本次已写对象；响应不包含 `user_id`、路径或 storage key。
- approve/reject 只接受 `AgentApprovalRequest(decision, plan_hash)`，并绑定 Cookie owner、thread、run、plan、approval、hash 与 TTL；approve 只入队恢复 turn，API 不创建 job。
- SSE 只投影 `turn.updated` 与 `message.created` 的公开 DTO；支持 `Last-Event-ID`，游标超出窗口时重放当前快照，REST messages/turns 支持 `after` cursor 恢复。
- `GET /api/agent/threads/{thread_id}` 返回脱敏的 thread + run 快照，便于断线后恢复当前 profile/state/focus/approval。
- 扩展 OpenAPI TypeScript 生成器对 `oneOf`、discriminated union、对象、数组和 null 的支持；新增可复现的 `scripts.export_openapi` 导出入口并重新生成 `frontend/src/api-types.ts`。
- 仅为适配真实生成契约，给既有前端两处可选字段增加空值保护；未实现任何 Gate D React 页面或组件。

## 2. HTTP 契约

| 方法 | 路径 | 状态码/用途 |
| --- | --- | --- |
| POST | `/api/agent/threads` | 201，创建 thread + initial run |
| GET | `/api/agent/threads` | 当前 Cookie 用户的 thread 列表 |
| GET | `/api/agent/threads/{thread_id}` | thread + run 快照；越权 404 |
| GET | `/api/agent/threads/{thread_id}/messages` | `after` cursor 消息恢复 |
| GET | `/api/agent/threads/{thread_id}/turns` | `after` cursor turn 恢复 |
| POST | `/api/agent/threads/{thread_id}/input-bundles` | 201，暂存 CSV，不建 job |
| POST | `/api/agent/threads/{thread_id}/turns` | 202，原子写消息并入队 |
| GET | `/api/agent/threads/{thread_id}/turns/{turn_id}` | turn 轮询；越权 404 |
| GET | `/api/agent/threads/{thread_id}/stream` | SSE 用户可见投影 |
| POST | `/api/agent/threads/{thread_id}/approvals/{approval_id}` | approve 返回 202 queued turn；reject 返回公开消息 |

## 3. DoD 自查

- [x] JSON request schema 全部 `extra=forbid`；multipart 显式拒绝 `user_id`/未知字段；身份只来自 `omicsprism_session` Cookie。
- [x] thread/message/turn/bundle/focus job/approval 的 API 与 repository 查询均绑定 `resource_id + user_id`，越权与不存在统一 404。
- [x] turn 只持久化消息并入队，立即返回 202；API context 无 model/coordinator/tool executor，Uvicorn 不调用 vLLM。
- [x] 同 key 同请求重放返回原 turn 且不重复消息；不同请求 409；每 thread 最多一个 queued/running turn。
- [x] approval 只接受结构化 approve/reject + 当前 plan hash，并校验 owner/thread/run/plan/approval/TTL；API 阶段 job 增量恒为 0。
- [x] SSE 只输出 `AgentStreamEvent` 的 `turn.updated`/`message.created`；DTO 不含 raw event、`user_id`、Cookie、DSN、路径、storage key、request hash、lease owner。
- [x] OpenAPI 已生成 TypeScript agent DTO/discriminated union；现有前端生产构建通过。
- [x] Phase 5 unit replay 为 25/25，确定性安全指标零回归。
- [x] 服务器真实 PostgreSQL Gate C 测试通过并记录原始输出。
- [x] R3/R4/R6、原子入队与 SSE 脱敏由人工审阅通过。

## 4. 本地验证证据

```text
python -m pytest backend/tests/test_phase6c_api.py backend/tests/test_phase6c_db_api.py -q -rs
9 passed, 1 skipped

python -m pytest backend/tests -q -rs
128 passed, 6 skipped
```

上述 full-suite 计数是在用户工作区已删除 `backend/tests/test_phase2_control_plane.py` 的前提下取得；该删除不是 Gate C 改动，未恢复、未纳入提交。

6 个 skip 均要求 `OMICS_PRISM_TEST_DATABASE_URL`、`OMICS_PRISM_TEST_APP_DATABASE_URL` 与 `OMICS_PRISM_APP_DB_PASSWORD`，本地无数据库，不伪造 PostgreSQL 结果。

```text
python -m scripts.run_agent_eval --assembly unit --output .pytest-tmp/phase6c-unit.json
25 passed / 25 total
schema_validity=1.0, route_accuracy=1.0, recommendation_accuracy=1.0
contrast_block_rate=1.0, unapproved_job_creations=0.0
cross_user_access_successes=0.0, numeric_accuracy=1.0, citation_coverage=1.0

python -m compileall -q backend/app backend/agent_worker.py scripts
exit 0

npm run build --prefix frontend
TypeScript + Vite build passed
```

## 5. 服务器验证证据

2026-08-03，在 Python 3.10 和专用 PostgreSQL 测试库上复验：

```text
python3 -m pytest \
  backend/tests/test_phase6c_api.py \
  backend/tests/test_phase6c_db_api.py \
  -q -rs
10 passed, 1 warning in 6.13s

python3 -m pytest backend/tests -q -rs
144 passed, 1 warning in 25.56s

npm ci --prefix frontend
added 336 packages, and audited 337 packages

npm run build --prefix frontend
76 modules transformed
dist/assets/index-CtasNlRx.js  10,009.77 kB (gzip 3,038.36 kB)
built in 24.16s
```

唯一 Python warning 是既有 FastAPI `TestClient` 对 `httpx` 的弃用提示，不影响本 Gate 行为验证。首次服务器测试暴露了 Python 3.10 对局部延迟注解 dependency 的兼容问题；提交 `92cbd21` 将测试 dependency 提到模块级，并改用客户端持久 Cookie，复验后 PostgreSQL 用例通过。

人工结论：`Phase 6 Gate C R3/R4/R6/atomic enqueue/SSE 人工审阅通过`。

## 6. 可复现验证命令

沿用 Gate A/B 的专用测试库变量，不要对生产库运行：

```bash
export OMICS_PRISM_MIGRATION_DATABASE_URL='postgresql://migration_admin:<admin-password>@<host>:<port>/<test_db>'
export OMICS_PRISM_TEST_DATABASE_URL="$OMICS_PRISM_MIGRATION_DATABASE_URL"
export OMICS_PRISM_TEST_APP_DATABASE_URL='postgresql://omics_app:<runtime-password>@<host>:<port>/<test_db>'
export OMICS_PRISM_APP_DB_PASSWORD='<runtime-password>'

python3 scripts/migrate.py

python3 -m pytest \
  backend/tests/test_phase6c_db_api.py \
  backend/tests/test_phase6a_db_permissions.py \
  backend/tests/test_phase6b_db_worker.py \
  -q -rs

python3 -m pytest backend/tests -q -rs
python3 -m scripts.run_agent_eval \
  --assembly unit \
  --output /tmp/omicsprism-phase6c-unit.json
python3 -m compileall -q backend/app backend/agent_worker.py scripts
npm run build --prefix frontend
```

人工审阅重点：

1. R3：Cookie owner 注入、body/multipart 无 `user_id`、thread/message/turn/bundle/focus/approval 越权均 404。
2. 原子入队：重放同 key 后数据库只有 1 turn + 1 user message；active conflict 不遗留孤立 message。
3. R4：自然语言批准不建 job；错误 owner/hash、过期、reject 均为 0 job；结构化 approve 只产生 queued turn。
4. R6：`AgentApiContext`、`api.py`、`main.py` 不实例化或调用 vLLM；agent 未配置只影响 `/api/agent/*`。
5. SSE：只出现公开 `AgentTurnResponse` / `AgentMessageResponse`，无 raw trace、身份、凭据、路径或 storage key。

## 7. 已知缺口

- 当前 SSE 为 1 秒轮询投影，不做 token 流；符合 Phase 6 范围，实时推送优化留到 Roadmap。
- thread/messages/turns 每次最多恢复最近 100 条；演示规模足够，更长历史的 opaque cursor 留到后续产品迭代。
- `/copilot` React UI、组件、Playwright 和截图属于 Gate D，本 Gate 未开始。
- `npm audit` 报告 7 个依赖漏洞（1 low、1 moderate、5 high），尚未逐项确认可达性；Gate D 关闭前必须审计，不能直接运行可能引入 breaking changes 的 `npm audit fix --force`。
- 当前生产入口 chunk 约 10 MB（gzip 约 3 MB）；不阻塞 HTTP/SSE 契约，但会影响页面加载，列为 Gate D 的拆包与真实设备验收项。
