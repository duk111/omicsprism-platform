# Phase 4 Report

## 当前状态

Phase 4 已正式关闭。worker 服务器完整测试、Phase 4 定向测试、编译检查和 R5/trace 人工审阅均已通过。

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
- [x] worker 服务器运行完整测试并执行 Phase 4 定向测试。
- [x] R5 grounded 回答与 trace 人工审阅通过。

## 本地验证

```text
72 passed, 2 skipped in 2.82s
Phase 4 core: 24 passed in 0.35s
compileall passed
git diff --check passed
```

两个 skip 是未设置专用 PostgreSQL 测试库环境变量时的既有权限测试。本地工作区仍有外部删除的 `backend/tests/test_phase2_control_plane.py`，因此服务器完整测试数量会更高；该删除未纳入本阶段提交。

## 服务器验证

```text
Phase 4 directed: 24 passed
full suite: 82 passed, 2 skipped, 1 warning
compileall exit: 0
R5/trace 人工审阅通过
```

服务器的两个 skip 与本地相同；唯一 warning 仍是 FastAPI/Starlette 对当前 `httpx` 兼容层的第三方弃用提示。服务器工作区中的 4 个交互方案 Markdown 删除和 `docker-compose.worker.yml` 未纳入 Phase 4 提交。

## 已知缺口

- 无 Phase 4 阻塞项。完整 golden eval/replay 和求职交付进入 Phase 5。
- 当前 trace 实现服务于受控 runtime/harness；管理界面必须等待真实管理员身份模型，不能用现有匿名 session 伪造管理员权限。
