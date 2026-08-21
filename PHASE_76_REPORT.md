# Phase 7.6（S6）只读工具多步自主循环

## 做了什么

- 新增结构化只读工具调用参数模型 `ToolCallArguments` / `ToolParamSet`，并扩展 `AgentAction.CALL_TOOL`。
- `ModelContext` 增加有界 `tool_history`；上下文构建保留最近结果，超过 32KB 时丢弃最老结果。
- `ProfilePolicyGuard.READ_ONLY_TOOLS` 明确排除 `SUBMIT_APPROVED_PLAN`；`DecisionValidator` 同时校验 profile 白名单和只读集合。
- `CHECK_INPUTS` 支持模型先调用 `get_analysis_spec` 等只读工具，再继续生成计划；提交和审批仍由原状态机显式执行。
- `ANSWER_WITH_EVIDENCE` 支持连续只读查询，保留现有 `EvidenceGrounder`、`AnswerVerifier` 和一次修复/降级链。
- 工具历史快照移除文件名、大小、存储键，避免 R1 存储元数据泄露。
- 新增 6 条 S6 测试，覆盖提交拒绝、profile 越权、结构化参数、历史预算、循环后审批闸门。

## DoD

- [x] 模型请求 `SUBMIT_APPROVED_PLAN` 被拒绝并写入 retry hint，不创建 job
- [x] interpretation profile 请求 `RUN_PREFLIGHT` 被拒绝
- [x] 只读循环可连续调用工具，工具结果进入有界 `tool_history`
- [x] 工具调用最多 4 次，超限正常收尾，不抛出预算异常
- [x] tool history 超出字节预算丢弃最老结果
- [x] 最终证据答案仍经过 grounding/verifier
- [x] 后端 `222 passed, 6 skipped`
- [x] unit eval `25/25`
- [x] 前端测试 `10 passed`，构建成功

## 已知边界

- 现有 `ModelAdapter` 的 S6 循环 schema 通过 `allow_tool_calls` 上下文标记启用；默认固定序列上下文和旧模型契约保持兼容。
- 前端构建继续报告既有 `InteractiveRouter` 大 chunk 警告，与 S6 无关。
