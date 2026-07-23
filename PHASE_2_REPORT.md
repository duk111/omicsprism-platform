# Phase 2 Report

## 当前状态

Phase 2 的 stub + fixture 控制平面已实现，自动化 DoD 已完成；Phase 2 尚未正式关闭，等待 R2/R4 红线人工审阅。

## 已实现

- `RuleRouter`：按意图与 focus 状态路由分析、解读、重跑、描述数据和模糊请求；描述数据不会自动触发分析。
- `ProfilePolicyGuard`：分析 profile 与解读 profile 使用固定白名单，解读 profile 结构上没有写工具。
- `InMemoryApprovalGate`：审批记录绑定 `run_id`、`user_id`、`plan_hash` 和 TTL，支持 pending/approved/expired 及共享存储重建。
- `InMemoryStateStore`：以 `run_id + user_id` 读取，checkpoint 使用乐观锁版本号，拒绝 stale writer 和跨用户覆盖。
- `MinimalContextBuilder`：只构造 `ModelContext`，包含用户消息、状态、focus job ID、摘要字段和 profile 工具名，不包含 DSN、凭据、句柄、原始路径或完整数据。
- `DecisionValidator`：校验状态转换和条件字段；`propose_plan` 必须有可行性判断，`not_answerable` 只能请求更多数据。
- `ScriptedModelAdapter`：明确命名的 fixture-only 决策队列，耗尽时显式失败，不冒充生产模型。
- `FixtureRunCoordinator`：单步 checkpoint 控制流跑通“路由 -> 计划 -> 审批 suspend -> 明确批准 resume -> 解读”，不创建真实业务 job；`created_job_ids` 恒为空。
- suspended run 只有明确批准语义才可 resume，普通追问不会被当作审批。

## DoD 自查

- [x] Stub + fixture 端到端跑通，且没有创建真实业务 job；`NEED_USER_INPUT` 澄清不会误触发审批。
- [x] 路由测试覆盖分析、结果追问、有/无 focus、重跑、描述数据和模糊意图。
- [x] 解读 profile 调用 `submit_approved_plan` 被结构性拒绝（R2）。
- [x] 未 resume、过期、plan hash 不匹配、非批准消息均不能通过；审批记录和 checkpoint 支持重建（R4）。
- [x] StateStore 乐观锁冲突和跨用户覆盖均确定性失败。
- [x] ContextBuilder 输出不含 DB、shell、凭据、原始路径、原始矩阵、完整 CSV 或完整日志。
- [x] 单步预算、非法状态转换和模型队列耗尽均确定性失败，不进入无限循环。
- [ ] R2/R4 红线人工审阅：待用户确认。

## 自动化验证

```text
56 passed, 2 skipped in 2.79s
```

两个 skip 是本机未配置真实 PostgreSQL 的权限测试，原因和 Phase 0/1 报告一致；服务器上的真实数据库权限证据不在本轮重复伪造。

定向 Phase 2 测试：

```text
10 passed
```

## 已知缺口 / 下一阶段

1. 仍未实现六个真实工具、真实 preflight、真实 job 提交与监控；这些属于 Phase 3。
2. 仍未实现 grounding、verifier、结果 adapter 和评测 fixture；这些属于 Phase 3。
3. 通过人工审阅后，才切换根契约到 Phase 3；在此之前不接入 vLLM 或真实工具。
