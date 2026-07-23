# Phase 1 报告

## 当前状态

Phase 1 已正式关闭。R1、R3、R6 的应用侧安全基线已经实现并完成本地自动化测试；
真实 PostgreSQL 角色权限和受限 DSN 下的手工业务回归已在 worker 服务器的专用
测试环境通过，Compose 展开检查和 R1/R3/R6 人工审阅均已完成。

## 已实现

- R3 身份隔离：`JobRepository.get(job_id, user_id)` 和
  `list_for_user(user_id)` 是面向用户的查询入口。API 路由只使用绑定用户的入口，
  不存在的任务和跨用户访问统一返回 404。worker、housekeeping 和文件存储内部调用
  使用名称明确的 `get_internal()` / `list_internal()`。
- R6 运行身份分离：移除 `PostgresJobRepository` 启动时建表逻辑；
  `003_runtime_jobs.sql` 负责 `jobs` DDL，并且只向 `omics_app` 授予
  `SELECT`、`INSERT`、`UPDATE`。API、worker、housekeeping 只读取
  `OMICS_PRISM_RUNTIME_DATABASE_URL`，一次性 `migrate` Compose profile 才持有
  migration/admin DSN。
- R1 模型边界：新增封闭的 `ModelContext` Pydantic 契约，只允许最小可序列化上下文。
  `StructuredModelAdapter` 将输出校验为 `AgentDecision`；首次无效时最多修复一次，
  修复后仍无效立即终止。`UnavailableModelAdapter` 明确报模型不可用，不生成伪造决策。
- 部署示例和环境变量模板已分离管理员迁移凭据与普通运行时凭据，并移除可直接使用的
  默认密码。没有引入 vLLM SDK、LangChain、LangGraph 或其他编排框架。

## DoD

- [x] repository 层和 HTTP API 层均有两用户互访测试；跨用户读取固定返回 404。
- [x] API、worker、housekeeping 的 Compose 配置均使用普通 `omics_app` runtime DSN；
  migration 使用独立管理员身份。
- [x] 模型只接受 `ModelContext`，额外的 DSN、原始路径、repository 等字段会被拒绝。
- [x] 模型输出必须通过 `AgentDecision` 校验，无效输出最多修复一次。
- [x] 模型未配置时，原有本地手工任务存取回归通过。
- [x] 在 worker 服务器运行专用 PostgreSQL 角色权限测试。
- [x] 在 `omics_app` DSN 下完成手工分析、任务处理、状态更新和结果页回归。
- [x] 在 worker 服务器完成关闭模型后的 API 和手工 DEG 回归。
- [x] 在 worker 服务器完成 Compose 配置校验。
- [x] R1/R3/R6 红线代码由人工审阅后关闭 Phase 1。

## 本地验证

```text
46 passed, 2 skipped in 4.50s
compileall passed
```

本地两个 skip 均为真实数据库权限测试，需要以下环境变量：

```bash
export OMICS_PRISM_MIGRATION_DATABASE_URL='postgresql://migration_admin:<admin-password>@<host>:<port>/<test_db>'
export OMICS_PRISM_TEST_DATABASE_URL="$OMICS_PRISM_MIGRATION_DATABASE_URL"
export OMICS_PRISM_TEST_APP_DATABASE_URL='postgresql://omics_app:<runtime-password>@<host>:<port>/<test_db>'
export OMICS_PRISM_APP_DB_PASSWORD='<runtime-password>'
python scripts/migrate.py
python -m pytest backend/tests/test_agent_db_permissions.py backend/tests/test_runtime_database_permissions.py -q -rs
```

## Worker 服务器数据库验证

2026-07-23 在 worker 服务器的 Python 3.10 虚拟环境和专用测试库执行。首次运行暴露
`datetime.UTC` 仅支持 Python 3.11 的兼容性问题；修复提交 `625c49b` 后重新执行通过。

实际迁移输出：

```text
applied 003_runtime_jobs.sql
```

实际权限测试输出：

```text
..                                                                       [100%]
2 passed in 3.63s
```

这两项测试证明：

- `omics_app` 是非超级用户，且不具备建库、建角色、复制或绕过 RLS 的能力；
- `omics_app` 可以对 `agent_runs` 执行所需的插入、读取和更新；
- `omics_app` 可以向 `agent_events` 追加和读取，但不能更新或删除事件；
- `omics_app` 可以创建、读取、列出和更新 `jobs` 业务记录；
- `omics_app` 不能修改 `jobs` 表结构，也不能删除 `jobs` 记录。

## Worker 服务器手工业务回归

2026-07-23 在 API 仅持有 `OMICS_PRISM_RUNTIME_DATABASE_URL`、未配置模型服务、
使用本地隔离执行器和文件目录的条件下完成一次真实 DEG 任务。

```text
health: HTTP 200
status: succeeded
progress: 100
error: None
result files: 115
images: 112
ZIP download: HTTP 200
ZIP validation: no errors detected in compressed data
cross-user job access: HTTP 404
```

`report_links.summary` 和 `report_links.interactive` 均为 `null`，这是当前 DEG
流程未生成可选 HTML 报告时的正常结果；结果页依赖的任务详情、结果文件、图片和
ZIP 均已验证可用。跨用户访问同一任务返回 404，证明 HTTP 层用户隔离在真实
runtime 数据库上生效。

## Worker 服务器 Compose 验证

2026-07-23 使用仅用于配置展开的占位密码运行 `docker compose --profile migration config`，
未启动、停止或重建服务。实际检查输出：

```text
runtime DSN count: 3
runtime users: ['omics_app', 'omics_app', 'omics_app']
migration DSN count: 1
migration users: ['postgres']
Compose database identity check: PASSED
```

这证明 API、worker、housekeeping 均只持有普通 runtime 身份，管理员 DSN 仅出现在
一次性 migration 服务中。

## 人工审阅

2026-07-23 完成 R1/R3/R6 红线人工审阅，审阅结论原文：

```text
Phase 1 R1/R3/R6 人工审阅通过
```

Phase 1 至此正式关闭，下一阶段为 Phase 2 Harness 单步循环、router、profile
白名单与审批恢复（stub 优先）。

## 已知缺口

- Live vLLM/Qwen 连通验证仍待 endpoint 和模型名明确后执行；本阶段没有伪造该结果。
