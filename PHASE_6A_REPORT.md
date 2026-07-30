# Phase 6 Gate A 报告：数据契约与红线测试

## 当前状态

Gate A 实现、本地验证与服务器真实 PostgreSQL 权限测试已完成。规定的人工红线审阅仍待确认，Gate B 尚未开始。

## 已完成工作

- 新增纯 SQL migration `004_agent_product_tables.sql` 与 `005_agent_product_roles.sql`，未引入 Alembic。
- 新增 thread、message、turn、input bundle/file、run state、plan、approval 与 append-only event 的 PostgreSQL repository。
- 在 `backend/app/agent/schemas.py` 集中新增有界 message block、thread/turn/bundle DTO、结构化审批请求、输入来源引用和 grounded answer 契约。
- 将 `AgentInputSourceRef` 纳入 canonical plan hash，并让 Phase 3-5 fixture/eval 通过 `compute_plan_hash()` 真实重算 hash。
- 实现资源读取的用户绑定、跨用户 not-found、turn 幂等、单 thread 最多一个 active turn、rejected/expired 审批终态，以及 append-only 数据库授权。

## 测试先行证据

Gate A 契约测试首次在实现前按预期于收集阶段失败：

```text
ModuleNotFoundError: No module named 'backend.app.agent.product_store'
```

随后新增的两条红线测试在修复前暴露了状态约束缺口：

```text
test_turn_idempotency_key_cannot_cross_thread_even_with_same_hash
test_rejected_approval_cannot_be_resumed
2 failed, 7 passed
```

最终本地 Gate A 定向测试结果：

```text
30 passed, 1 skipped in 0.50s
```

被跳过的是需要专用服务器测试库的真实 PostgreSQL 角色/ownership 测试。

## 本地验证

```text
python -m pytest backend/tests -q -rs
103 passed, 3 skipped in 3.47s

python -m compileall -q backend/app scripts
compileall exit: 0

python -m scripts.run_agent_eval --assembly unit
25 passed, 0 failed, pass_rate 1.0
unapproved_job_creations 0.0
cross_user_access_successes 0.0
numeric_accuracy 1.0
citation_coverage 1.0
```

三个 skip 都需要真实 PostgreSQL 环境变量；本报告不声称本地数据库测试已经通过。

## Gate A DoD

- [x] Repository 读取同时绑定 resource id 与 user id；本地跨用户访问返回 not-found。
- [x] 产品请求 schema 使用 `extra=forbid` 并拒绝客户端传入 `user_id`。
- [x] 相同幂等请求只返回一个 turn；request、thread 或 run 改变时返回冲突。
- [x] SQL 授权规定 `agent_messages`/`agent_events` 仅有 SELECT/INSERT，所有产品表均无 DELETE 权限。
- [x] 单元测试证明未审批、过期、hash 不匹配、已拒绝和跨用户审批均保持 job 创建数为 0。
- [x] 已在服务器专用测试库应用 migration 004/005，并通过真实 PostgreSQL 权限测试。
- [ ] 完成人工 R3/R4/append-only 审阅。

## 服务器验证

在仓库根目录使用前序 Phase 的同一套专用测试库凭据执行：

```bash
export OMICS_PRISM_MIGRATION_DATABASE_URL='postgresql://migration_admin:<admin-password>@<host>:<port>/<test-db>'
export OMICS_PRISM_TEST_DATABASE_URL="$OMICS_PRISM_MIGRATION_DATABASE_URL"
export OMICS_PRISM_TEST_APP_DATABASE_URL='postgresql://omics_app:<runtime-password>@<host>:<port>/<test-db>'
export OMICS_PRISM_APP_DB_PASSWORD='<runtime-password>'

python scripts/migrate.py
python -m pytest \
  backend/tests/test_agent_db_permissions.py \
  backend/tests/test_runtime_database_permissions.py \
  backend/tests/test_phase6a_db_permissions.py \
  -q -rs
```

首次应用时应出现：

```text
applied 004_agent_product_tables.sql
applied 005_agent_product_roles.sql
```

不要把数据库密码贴进报告；服务器运行后只补 migration 名称和 pytest 结果。

### 服务器输出

2026-07-30 在 worker 所在服务器执行：

```text
python3 scripts/migrate.py
applied 004_agent_product_tables.sql
applied 005_agent_product_roles.sql

python3 -m pytest \
  backend/tests/test_agent_db_permissions.py \
  backend/tests/test_runtime_database_permissions.py \
  backend/tests/test_phase6a_db_permissions.py \
  -q -rs
... [100%]
3 passed in 12.81s
```

### 人工审阅

待完成：Phase 6 Gate A R3/R4/append-only 人工审阅。

## 已知缺口

- 本工作站缺少数据库环境变量时相关测试按契约明确 skip；真实 constraints/grants 已在服务器专用测试库验证通过。
- Gate B 的生产协调器和 agent worker 按范围尚未实现。
- HTTP/SSE 路由与前端接入分别属于 Gate C/D，当前按契约不存在。
