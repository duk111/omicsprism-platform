# Phase 4 Report

## 当前状态

Phase 4 的本地实现和自动化测试已完成第一版，尚未正式关闭。待完成项为 worker 服务器验证和 R5 人工审阅。

## 已实现

- `query_result_evidence` 在每个返回行中保留源 CSV 的 `_row_id`，使 citation 可以定位到实际数据行；仍只读取既有白名单 artifact 与字段。
- `EvidenceGrounder` 只接受成功的 `query_result_evidence` 输出；所有 claim 的 artifact、checksum、row_id 必须匹配当前证据。无结果时固定输出“没有满足阈值的证据”。
- `EvidenceGrounder` 拒绝从 `union_significant_genes.csv` 得出上调/下调方向结论，并在验证第二次失败后降级为原始证据行和免责声明。
- `AnswerVerifier` 不持有工具或存储句柄；它确定性核验引用、claim 数字和因果断言，失败草稿只允许修复一次。
- `GroundedAnswerPipeline` 固化“验证 -> 修一次 -> 原始证据降级”路径。
- `FixtureRunCoordinator` 支持 `AWAIT_FOLLOWUP` 保留 interpretation focus；重跑意图在调用模型前强制切回 analysis / `CHECK_INPUTS`。
- `InMemoryAgentEventStore` 只提供 append/list，按 `run_id + user_id` 查询；trace payload 拒绝凭据、原始数据路径等敏感键。harness 对 route/state 追加安全审计事件。

## DoD 自查

- [x] 每条最终 claim 都有 artifact/checksum/row_id 引用；引用与当前证据逐项一致。
- [x] 无法在引用行核验的数字、无效行号和因果断言均被 verifier 拒绝；第二次失败降级为原始证据。
- [x] 返回 0 行时仅输出“没有满足阈值的证据”。
- [x] `union_significant_genes.csv` 不能产出带方向的结论；结果读取的跨用户 404 保持由 Phase 3 repository 边界保证。
- [x] 同一 focus 的追问保留 job；重跑在调用模型前切回 analysis profile。
- [x] 事件接口只有 append/list，trace 查询绑定 `run_id + user_id`，且拒绝敏感 payload 键。
- [ ] worker 服务器运行完整测试并执行 Phase 4 定向测试。
- [ ] R5 grounded 回答与 trace 人工审阅通过。

## 本地验证

```text
72 passed, 2 skipped in 2.82s
Phase 4 core: 24 passed in 0.35s
compileall passed
git diff --check passed
```

两个 skip 是未设置专用 PostgreSQL 测试库环境变量时的既有权限测试。本地工作区仍有外部删除的 `backend/tests/test_phase2_control_plane.py`，因此服务器完整测试数量会更高；该删除未纳入本阶段提交。

## 已知缺口

- 不接入 vLLM、完整 golden eval/replay、对外聊天 UI 或独立多 agent 服务，这些不属于 Phase 4。
- 当前 trace 实现服务于受控 runtime/harness；管理界面将在有真实管理员身份模型后再接入，不能用现有匿名 session 伪造管理员权限。
