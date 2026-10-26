# PR9：意图路由与上下文优化交付

日期：2026-10-26（Asia/Shanghai）。

本轮保留现有 LangGraph、Agent 角色、只读 Result QA 工具和确认/执行边界；没有增加记忆数据库、Agent 或分析参数。

## 修改文件

- `backend/app/agent/router.py`：知识问题优先于 `padj`/结果词；明确分析操作优先；pending 参数修改优先；多 Job 代词请求确定性澄清；无 Job 结果请求仍进入 Result QA 安全拒答；路由 classifier 的模型预算计数。
- `backend/app/agent/graph.py`：增加有 owner 约束的 `ResultSelection` 查询定位状态。
- `backend/app/agent/context.py`：Result QA 只暴露当前 focus Job 的 artifact 和已验证查询定位；focus 不一致时丢弃旧 selection。
- `backend/app/agent/runtime.py`：显式切换 focus 时更新 `current_job`、裁剪 recent jobs、清空旧实体/引用/聊天窗口和查询定位。
- `backend/app/agent/nodes/main.py`：成功的 `query_artifact` 保存 Job、artifact、checksum、查询参数、行号和有限实体索引；连续追问先重查原 artifact，再从原记录集合比较，后续答案仍必须重新走证据验证。
- `frontend/src/copilot/CopilotPage.tsx`：URL 中显式 `job_id` 作为当前 focus 发送，避免页面切换继续沿用旧任务。
- `backend/app/agent/result_context.py`：集中处理授权 Job scope、deictic follow-up 和有界查询 locator。
- `scripts/run_pr9_eval.py`：PR9 场景化 AgentEvalV2 Recorded/Scripted Model runner，记录路由、继承、切换隔离、reroute、模型调用和延迟。
- `backend/tests/test_agent_intent_context.py`：PR9 中英文路由、pending 修改、多 Job 澄清、Job 切换隔离、selection 所有权回归。
- `backend/tests/test_agent_artifact_e2e.py`：真实落盘查询保存 selection 的 checksum/row ids 回归。
- `reports/pr9_intent_context_audit.md`：阶段 A 调用链、问题、最小方案和测试计划。

## 路由与上下文对比

修复前，`padj`、`fold change`、`分析` 等词可能在知识/操作语句中抢先命中结果或分析路径；pending 计划修改也可能被结果关键词覆盖。修复后，显式知识问题（“什么是火山图”“DEG 中 padj 表示什么”）进入 QA；当前有授权 Job 的结果问题进入 Result QA；“配置 DEG”“运行 DEG”及 pending 参数修改进入 Analysis；多 Job 的“解释一下这个/刚才那个结果”不会猜 Job。

修复前，focus 改变只追加 recent jobs，旧 `current_job`、实体、citation 和聊天提示可能继续影响下一轮。修复后，显式 focus 是权威选择，切换会更新 current job 并清空旧结果定位；ResultSelection 只保存真实工具返回的 checksum、artifact、查询参数和 row ids，模型历史文本不能成为证据。

## 验证记录

- PR9 + router/grounding/runtime/graph/confirmation/PR7/PR8 定向：**227 passed, 11 skipped**（其中 PR9 场景回归 42 passed；路由/图/证据核心 173 passed, 9 skipped）。
- 结果 artifact 端到端和 PR7 evidence/PR8 DEG、DEM、GMA 查询：**97 passed**。
- 全后端：**528 passed, 14 skipped, 1 xfailed, 1 failed**。唯一失败是既有 `test_evaluator_self_tests_keep_malformed_evidence_out_of_agent_quality_metrics`，metadata/contrast capability 仍为 4/6；PR8 报告已记录同一基线差异，未修改科学或证据断言来掩盖它。
- AgentEvalV2 Recorded/Scripted Model 当前质量场景：release gate **passed**，task terminal success **1.000**，pass@1 **1.000**，multi-turn memory **1.000**，citation/numeric consistency **1.000**，unsupported claim **0**，模型调用 **59**，p95 turn **约 31.7 ms**。这是确定性 fixture/Scripted Model 指标，不代表真实模型语义效果或线上延迟。
- PR9 专用前后对比（15 个场景）在 `reports/pr9-scenarios-before.json` / `reports/pr9-scenarios-after.json`：case pass **0.417 → 1.000**，intent routing **0.500 → 1.000**，Result QA routing **0.667 → 1.000**，multi-turn inheritance **0 → 1.000**，Job switch isolation **0 → 1.000**，model calls **21 → 11**，平均每轮 **1.40 → 0.73**；reroute 均为 0。Recorded Model 不代表线上模型。

## 仍有风险

真实模型仍可能把没有明确实体名的单 Job 指代解析为错误实体；selection 只提供安全定位，追问会实际重查并校验 artifact、checksum、原始行号。URL focus 只在前端页面显式带 `job_id` 时更新，后端 API 和执行时都要求 focus Job 通过 owner 校验。AgentEvalV2 capability metadata/contrast 的 4/6 基线缺口仍需单独修复；本轮没有扩大其范围。

## Git Diff 摘要

路由、已有 State/ContextAssembler、runtime focus 合并和主循环安全边界为小范围修改；新增一个有界查询定位模型和 PR9 回归测试，未修改科学计算核心、任务执行系统、数据库 schema 或 LangGraph 拓扑。
