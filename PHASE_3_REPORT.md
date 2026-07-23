# Phase 3 Report

## 当前状态

Phase 3 已正式关闭。真实 DEG/GMA fixture 已由 worker 服务器任务采集，R2/R3/R4 人工审阅已通过。

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
- `scripts/capture_agent_fixtures.py` 只从真实 job 输出截取最小 CSV，并记录源文件和 fixture checksum；CSV 固定写入 LF，`.gitattributes` 保证跨平台检出后仍可核验。

## DoD 自查

- [x] 6 个工具不存在 `NotImplementedError`、硬编码成功或通用 SQL/文件/shell 逃生口。
- [x] 无有效 contrast 时 job 创建数为 0。
- [x] 未审批、过期审批、计划参数变化和 hash 不匹配时 job 创建数为 0（R4）。
- [x] 同一 `idempotency_key` 重放返回同一 job，repository 只新增一次。
- [x] 解读 profile 调写工具在 handler 前被 `PolicyGuard` 拒绝（R2）。
- [x] job/result 查询绑定 `resource_id + user_id`，跨用户保留 404（R3）。
- [x] 工具输出不超过 50 行和 32KB，CSV 按字段名读取。
- [x] worker 服务器采集真实 DEG/GMA fixture，并记录源文件 checksum。
- [x] R2/R3/R4 红线人工审阅通过。

## 本地验证

```text
worker: 74 passed, 2 skipped, 1 warning in 0.82s
compileall exit: 0
git diff --cached --check passed
```

两个 skip 是未设置专用 PostgreSQL 测试库环境变量时的 Phase 0/1 权限测试；该库已在此前服务器验收中通过。唯一 warning 来自 FastAPI/Starlette 对当前 `httpx` 兼容层的第三方弃用提示，不属于 Phase 3 实现。

真实 fixture 已人工确认可公开提交，均截取表头和 20 行数据：

| 类型 | source job | artifact | source checksum | fixture checksum |
| --- | --- | --- | --- | --- |
| DEG | `efdf997e-21ae-4430-a117-7c0c475b8ce9` | `differential_gene_counts.csv` | `sha256:da1d07cc03ea77e514852c2539e08d1cb80df6f1e0cf64a9b4054b65711c6cb3` | `sha256:3f8bedac699700ca825acf5e20ce59f8de6783b29c28e5e7433df8c8d4bba317` |
| GMA | `8e39b468-e6ca-4948-8dee-a570382b85e6` | `T02_High_Confidence_Network.csv` | `sha256:6557d261a2b1bc7e80031cdc5adc4a6f12c07e612beacf1cd194ec37e3b8c9bb` | `sha256:6e38154af40abaed4f971717b5dcb4d4108fcc1aef19021b7b6dd1e0f82f03be` |

Phase 3 定向测试覆盖：有效/无效 contrast、`same_fields` 分层、未审批、过期审批、参数篡改、幂等重放、跨用户 404、profile 写拒绝、结果表白名单、字段过滤/排序与输出裁剪。

## 已知缺口

无 Phase 3 阻塞项。Phase 4 将实现 grounded 回答、验证者、多轮 focus 与 trace；不在本阶段接入 vLLM 或完整评测。
