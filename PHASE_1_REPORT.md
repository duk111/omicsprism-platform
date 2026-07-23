# Phase 1 报告

## 当前状态

Phase 1 进行中。R1、R3、R6 的应用侧安全基线已经实现并完成本地自动化测试；
真实 PostgreSQL 角色权限和受限 DSN 下的完整业务回归仍需在 worker 服务器的
专用测试库执行，本文不将这些尚未执行的项目标记为通过。

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
- [ ] 在 worker 服务器运行专用 PostgreSQL 角色权限测试。
- [ ] 在 `omics_app` DSN 下完成手工分析、任务处理、状态更新和结果页回归。
- [ ] 在 worker 服务器完成 Compose 配置校验和关闭模型后的 API 回归。
- [ ] R1/R3/R6 红线代码由人工审阅后才能关闭 Phase 1。

## 本地验证

```text
45 passed, 2 skipped in 3.83s
compileall passed
```

两个 skip 均为真实数据库权限测试，需要以下环境变量：

```bash
export OMICS_PRISM_MIGRATION_DATABASE_URL='postgresql://migration_admin:<admin-password>@<host>:<port>/<test_db>'
export OMICS_PRISM_TEST_DATABASE_URL="$OMICS_PRISM_MIGRATION_DATABASE_URL"
export OMICS_PRISM_TEST_APP_DATABASE_URL='postgresql://omics_app:<runtime-password>@<host>:<port>/<test_db>'
export OMICS_PRISM_APP_DB_PASSWORD='<runtime-password>'
python scripts/migrate.py
python -m pytest backend/tests/test_agent_db_permissions.py backend/tests/test_runtime_database_permissions.py -q -rs
```

Phase 1 关闭前应把以下实际输出追加到本文：

```text
applied 003_runtime_jobs.sql
2 passed
```

## Worker 服务器验证步骤

1. 使用专用测试数据库，不要直接在生产库首次验证。
2. 用管理员 DSN 运行 `python scripts/migrate.py`，保留输出。
3. 设置两个 `OMICS_PRISM_TEST_*_DATABASE_URL`，运行上述两项权限测试。
4. API、worker、housekeeping 只设置指向 `omics_app` 的
   `OMICS_PRISM_RUNTIME_DATABASE_URL`，不要向这些进程暴露管理员 DSN。
5. 提交一个手工任务，确认 worker 能更新状态，并检查任务列表、任务详情和结果页。
6. 不配置模型服务，重复一次手工任务的提交、列表和读取回归。
7. 运行 `docker compose config`，确认 API/worker/housekeeping 展开后的数据库用户名均为
   `omics_app`，只有 `migrate` 服务使用管理员用户名。

## 已知缺口

- 尚未提供 vLLM/Qwen endpoint 和模型名，因此没有伪造 live model 结果；真实模型连通验证待配置
  明确后执行。
- 当前 Windows 工作站没有专用 PostgreSQL 测试 DSN，也没有 Docker CLI，所以数据库权限和
  Compose 实测仍保持未完成状态。
