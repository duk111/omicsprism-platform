# Phase 3 Report

## 当前状态

Phase 3 本地实现和自动化测试已完成第一版，尚未正式关闭。待完成项是 worker 服务器真实 DEG/GMA fixture 采集，以及 R2/R3/R4 红线人工审阅。

## 已实现

- 6 个固定工具均通过 `AgentToolRuntime` 接入真实依赖；未配置运行时会抛 `ToolConfigurationError`，不再保留 `NotImplementedError` 或假成功。
- `inspect_uploaded_inputs` 返回列名、维度、dtype、min/max、负值、整数比例、缺失率与分组重复数。
- `AnalysisSpecRegistry` 继续使用现有 `differential` / `dem` / `correlation`，并集中提供默认参数生成 `effective_params`。
- `build_contrast_preview` 按 `same_fields` 分层，确保 tested/reference 同时存在且各自满足 `min_replicates`；无有效 contrast 不可提交。
- `submit_approved_plan` 在副作用前复跑 preflight、重算 `plan_hash`、验证用户和审批 TTL，并用确定性 job ID 实现幂等提交。
- `JsonPlanStore` / `JsonApprovalGate` 支持单机部署进程重建；跨用户读取表现为不存在。
- `PolicyToolExecutor` 在调用 handler 前执行 profile 白名单；解读 profile 结构上不能调用写工具。
- `get_jobs_status` 只调用用户绑定的 repository 接口，并返回裁剪后的日志摘要。
- `query_result_evidence` 按分析类型限制结果表，按字段名过滤/排序，保留 repository 的跨用户 404，并限制完整 `ToolResult` 不超过 50 行和 32KB。
- `scripts/capture_agent_fixtures.py` 只从真实 job 输出截取最小 CSV，并记录源文件和 fixture checksum；当前未伪造 fixture。

## DoD 自查

- [x] 6 个工具不存在 `NotImplementedError`、硬编码成功或通用 SQL/文件/shell 逃生口。
- [x] 无有效 contrast 时 job 创建数为 0。
- [x] 未审批、过期审批、计划参数变化和 hash 不匹配时 job 创建数为 0（R4）。
- [x] 同一 `idempotency_key` 重放返回同一 job，repository 只新增一次。
- [x] 解读 profile 调写工具在 handler 前被 `PolicyGuard` 拒绝（R2）。
- [x] job/result 查询绑定 `resource_id + user_id`，跨用户保留 404（R3）。
- [x] 工具输出不超过 50 行和 32KB，CSV 按字段名读取。
- [ ] worker 服务器采集真实 DEG/GMA fixture，并记录源文件 checksum。
- [ ] R2/R3/R4 红线人工审阅通过。

## 本地验证

```text
64 passed, 2 skipped in 2.73s
compileall passed
git diff --check passed
```

两个 skip 是需要专用 PostgreSQL 测试库的 Phase 0/1 权限测试。当前工作区中 `backend/tests/test_phase2_control_plane.py` 仍处于外部删除状态，未计入本次本地测试；远端提交 `4fef219` 中保留了该文件，本轮不会把删除纳入提交。

Phase 3 定向测试覆盖：有效/无效 contrast、`same_fields` 分层、未审批、过期审批、参数篡改、幂等重放、跨用户 404、profile 写拒绝、结果表白名单、字段过滤/排序与输出裁剪。

## 待服务器验证

1. 拉取 Phase 3 代码后运行完整测试。
2. 使用现有小型 DEG/GMA job，或新跑一个小任务。
3. 用 `scripts/capture_agent_fixtures.py` 截取真实结果表并将生成的 CSV 与 `manifest.json` 提交回来。
4. 人工审阅 R2/R3/R4 后再关闭 Phase 3。
