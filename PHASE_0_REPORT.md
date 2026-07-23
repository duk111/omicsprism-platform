# Phase 0 Report

## 当前结论

Phase 0 的本地代码、契约与测试骨架已完成；真实 PostgreSQL 权限测试尚未在服务器执行，因此本 Phase **尚未正式关闭**。本报告不伪造数据库通过结果。

## 做了什么

- 建立 `backend/app/agent/` 包骨架，并固定各模块的真实接口签名。
- 在 `schemas.py` 集中定义 `RouteDecision`、`AgentDecision`、`RunState`、`ToolResult`、`GroundedAnswer`、`VerifierVerdict` 及其嵌套契约。
- 建立共享 `AnalysisSpecRegistry`；内部唯一分析类型继续使用现有的 `differential` / `dem` / `correlation`，展示标签集中映射为 DEG / DEM / GMA。
- 注册六个工具名；所有工具均显式抛出 `NotImplementedError`，没有返回假数据。
- 新增 `001_agent_tables.sql`、`002_agent_roles.sql` 和按文件名执行、通过 `schema_migrations` 记账的 `scripts/migrate.py`。
- 新增普通数据库角色 `omics_app` 的 SQL：该角色不是超级用户、不能建库、不能建角色、不能复制、不能绕过 RLS。
- 应用层 `AgentEventStore` 只暴露 `append` 与 `list_for_run`，没有 UPDATE / DELETE 接口。
- 建立项目 `.venv`，安装 pytest、pytest-asyncio 和 httpx，并新增 `backend/requirements-dev.txt`。

## DoD 自查

- [x] schema 单测通过：合法样例可解析；非法枚举值、未知字段和缺必填字段被拒。
- [x] `001_agent_tables.sql` / `002_agent_roles.sql` 已写。
- [x] append-only 应用层接口测试、SQL 权限静态检查和真实 PostgreSQL 权限测试均已写。
- [x] 无数据库时，真实权限测试明确显示为 skip，并说明所需环境变量。
- [ ] 在服务器 PostgreSQL 上实跑权限测试并把原始输出贴入本报告。
- [x] `ToolRegistry` 六个工具名齐全，逐个调用均抛 `NotImplementedError`。
- [x] 未引入 vLLM、模型调用依赖、LangChain、LangGraph 或其他编排框架。

## 本地测试原始输出

```text
..s............................                                          [100%]
=========================== short test summary info ===========================
SKIPPED [1] backend\tests\test_agent_db_permissions.py:15: 需要 OMICS_PRISM_TEST_DATABASE_URL、OMICS_PRISM_TEST_APP_DATABASE_URL 和 OMICS_PRISM_APP_DB_PASSWORD 才能验证真实 PostgreSQL 权限
30 passed, 1 skipped in 0.30s
```

执行命令：

```powershell
cd omicsprism-platform
.venv\Scripts\python.exe -m pytest backend\tests -q -rs
```

## 服务器 PostgreSQL 权限验证

当前本机没有 Docker、PostgreSQL、`psql`，且本地 5432 端口不可达，因此没有执行真实权限测试。

服务器执行前设置专用测试数据库连接，不要指向生产数据库：

```powershell
$env:OMICS_PRISM_TEST_DATABASE_URL = "postgresql://<migration-admin>:<password>@<host>:5432/<test_db>"
$env:OMICS_PRISM_TEST_APP_DATABASE_URL = "postgresql://omics_app:<password>@<host>:5432/<test_db>"
$env:OMICS_PRISM_APP_DB_PASSWORD = "<password>"
.venv\Scripts\python.exe -m pytest backend\tests\test_agent_db_permissions.py -q -rs
```

测试会确定性验证：

- `omics_app` 的超级用户等危险属性全部为 false。
- `agent_runs` 可 INSERT / SELECT / UPDATE。
- `agent_events` 可 INSERT / SELECT。
- `agent_events` UPDATE 抛 `InsufficientPrivilege`。
- `agent_events` DELETE 抛 `InsufficientPrivilege`。

### 服务器原始输出

> 待在服务器执行后粘贴。当前没有输出，Phase 0 因此尚未正式关闭。

## 红线人工审阅点

- `agent_events` 的 owner 是执行 migration 的管理员角色，不是 `omics_app`。
- `omics_app` 仅获得 `agent_events` 的 SELECT / INSERT，并显式撤销 UPDATE / DELETE / TRUNCATE / REFERENCES / TRIGGER。
- API 和 worker 的 DSN 本阶段没有修改；切换到 `omics_app` 属于 Phase 1 安全基线。
- 角色密码不写入 SQL 或仓库，由 migration runner 通过会话设置传入 `002_agent_roles.sql`。

## 已知缺口

1. **Phase 1 第一项任务**：新增 `JobRepository.get(job_id, user_id)`，agent 侧只允许调用带用户条件的新方法，旧 `get(job_id)` 逐步收敛。
2. **Phase 1 安全基线**：API 和 worker 的 DSN 从 PostgreSQL 超级用户切换到 `omics_app`，并验证原有手工业务不受影响。
3. 服务器 PostgreSQL 权限测试尚未执行，原始输出待补。
4. 本地安装完整 `backend/requirements-dev.txt` 时网络请求超时；pytest、pytest-asyncio 和 httpx 已安装并可运行测试，但本机仍没有可用 PostgreSQL 环境。
