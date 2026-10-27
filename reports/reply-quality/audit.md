# Agent 回复质量审计（修改前）

日期：2026-10-26（Asia/Shanghai）。代码基线：`b5e5a66cb4faf04364dd1d45d3509a2da7986e38`。

本报告在修改任何产品代码前形成。20 问真实回复、原始模型消息、工具参数/返回、上下文、最终 blocks、延迟、模型调用数和验证结果见 `baseline.json`。`baseline-answers.md` 是逐题原文及人工审阅。新增脚本仅为离线评估入口，不进入产品运行路径。

## 测试条件与边界

- 实际调用部署文档指定算力服务器上的 `/v1/models` 和 `/v1/chat/completions`，服务发现确认 `Qwen3-14B-AWQ`（root `Qwen/Qwen3-14B-AWQ`），不是 Scripted Model。
- 使用生产 `VllmGraphModel`、LangGraph、capability registry、Job 所有权检查、磁盘 Artifact 查询、GroundedAnswerPipeline 和 AnswerVerifier。与 bootstrap 一致：temperature=0、max_tokens=768、enable_thinking=false、structured_tool_response=false。
- DEG、DEM、GMA 使用已有 `fixtures/result_query` 捕获的真实 Worker 输出。临时 Job 与文件均隔离，不访问生产任务，不运行科学分析。Analysis 问题处于无上传 bundle 场景；用户提及文件不等于系统已收到文件。
- 此处评估本地现有链路调用部署模型，不代表生产 API/队列/浏览器的全链路验收。服务可直接访问，未改隧道、服务器或部署配置。
- 初版采集脚本错误地假设所有工具返回均有 `tool` 字段；该次运行无效，排除，修正后完整重跑 20 问。无效原始运行保留为 `harness-invalid.json`，不得纳入质量或延迟统计。
- 每题单次调用，temperature=0 仍不能保证服务完全确定。延迟只作配对观察，不作显著性推断。

## 生成链路与根因

| 位置 | 当前行为 | 质量影响及分类 |
| --- | --- | --- |
| `model.py::_QA_SYSTEM_PROMPT` | 要求只使用 recent_messages 与 dataset role names；未区分一般知识与用户数据事实 | 一般科学知识的回答边界容易被误解（提示词） |
| `model.py::_ANALYSIS_SYSTEM_PROMPT` | 强调参数与控制动作，但只读概念解释和配置动作的区别不够具体 | 概念问题可能落到规划流程（模型与上下文） |
| `model.py::_RESULT_QA_SYSTEM_PROMPT` | 声明 typed decision，但未给最终对象/Verifier 接受句式的具体例子 | r01 返回 `{gene_id,padj}` 而非 decision，两次校验失败；r03 等返回普通 prose 后被替换（模型格式） |
| `nodes/main.py::_run_agent_loop` | 结果域的 answer/ask_user 有证据时用 pipeline 原始行；无证据时拒答 | q02、q06 模型原文是概念解释，最终却变为要求 Job 的拒答；不应放行未绑定数据 prose（上下文/安全约束） |
| `nodes/analysis.py::_missing_inputs_message` | 已选定 DEG/DEM 时仍展示全部模块与输入及下一步 | a02/a03 冗长，主问题被流程文案淹没（确定性渲染） |
| `grounding.py` / `verifier.py` | 原始行或有限的实体/字段/数值句式；未知解释、派生数值、多行归因均 fail closed | r03 模型解释含约 3.9 倍等派生/舍入值，不能直接作为已验证结论放行；r08 普通因果说明不在允许语法内（验证约束） |
| `nodes/main.py::_grounded_answer_text` / `nodes/result_qa.py::_answer_text` | 拼接 claim 原文，再同时创建 text 和相同 claim 的 evidence block | r03/r04/r06/r07/r08/r09 逐行机械展开、同一证据在 UI 展示两次（后端投影；前端只是忠实渲染） |
| `result_context.py::is_result_followup` | 英文 `its` 命中追问，缺 selection 时直接退出 | r02 是完整新问题却 0 次模型调用且返回中文澄清；不是模型输出问题（前置上下文） |
| `router.py` | q02/q06 路由 Result QA，q05 路由 Analysis | 路由词触发了错误角色。可先让现有角色用已有 reroute 修正概念题；不改路由规则/追问机制 |

## 已确认的真实案例

- q02：用户问 padj 定义与效应量。模型解释了多重检验与效应量区别，但最终回复“尚未取得可验证的结果证据……”。无数据结论需求，不应强制用户提供 Job。
- q05：用户问 log2FoldChange 的符号与比较方向。模型有相关解释，但 Analysis 的确定性归一化将它替换为三个模块/上传文件说明。
- r01：已查到 `gene_up`、`padj=4.335429232883268e-101`；模型生成裸 JSON 而非现有 decision schema，最终为英文通用失败提示。
- r03：用户只要求两个统计值及含义。最终完整输出九个字段，解释消失。r08 同样丢失“能否证明因果”的回答。两题的数值证据本身正确。
- r05：模型把 `result_view` 当成 Artifact 路径，失败后又声称不存在；边界拒绝该断言是正确的。应在提示词明确先发现真实路径，错误不等于空匹配。
- a01：模型将 GMA 描述为推断调控关系，未说明关联不能证实调控。应收紧科学表述。

## 最小实施范围（基线固化后）

1. 仅补充现有 QA、Analysis、Result QA 提示词：先答意图、语言一致、概念题复用已有 reroute、清晰的最终输出对象、查询成功后的简洁证据句式；禁止将错误当空结果，禁止舍入/派生数值混入 claim。
2. 复用现有 GroundedAnswerPipeline/AnswerVerifier，按用户明确字段生成简洁、逐行绑定的确定性候选句，再执行原验证。未知字段/双实体 GMA/字符串等保持可审计原始行，不推断方向或显著性。验证拒绝仍保留原始证据与明确说明。
3. 复用现有回复投影入口，补充独立的一般统计术语说明；不从文件名或数值臆造机制、因果或比较方向。模型生成的数据 prose 继续受原验证约束。
4. 消除正文与 evidence block 的完全重复，保留全部引用路径、checksum 与原始行号，不增加 UI 功能。
5. 精简已确定分析模块的缺输入文案，不改变参数推断、确认、权限或任务执行。

最初实施边界：不改算法、Agent/工具/工作流/调用阶段、记忆系统、路由与追问选择逻辑、前端功能、验证器或证据权限。r02 前置追问误判无法靠提示词修复。

验收后的范围修正（基线保持不变）：真实复测确认概念题在进入模型前已被分错角色，单靠提示词无法修复。因此仅在原有 definition 判断补齐“是什么／说明什么／如何解释”的中文同义表达；在原追问识别排除同一句自包含的 which/what gene/metabolite 问题，同时保留 among them 等子集追问。仍沿用原路由、原选择和原证据校验机制，没有新增 Agent、工具、workflow 或模型阶段。该修正针对已审计的问题，并以当前结果问法及子集追问作反向回归。

## 原有测试基线

定向回归（新增回复质量、运行时、evidence reliability、result query semantics、artifact E2E）：**129 passed、2 skipped**。前端 Vitest：**28 passed**。后端全量：**556 passed、14 skipped、1 xfailed、1 个既有失败**；既有失败为 `test_agent_eval_v2.py::test_evaluator_self_tests_keep_malformed_evidence_out_of_agent_quality_metrics`，在未修改的基线 detached worktree 上同样复现，capability isolation pass@1 为 0.6666666666666666 而测试期望 1。该失败与本轮回复投影、提示词、路由修补无关。保留 FastAPI on_event、Pydantic 等既有弃用警告。新增测试没有放宽权限、证据或任务断言。

后端全量在最后一次修复空结果 `response_text` 后重跑，仍仅剩上述既有 evaluator self-test 失败；空表拒绝模型结论和全部回复质量回归均通过。

同一 20 问的修改后真实采集在 `after-final.json`，汇总对比在 `comparison.md`，原始模型消息、工具参数与证据全部保留。两次延迟复测也保留；第二次复测修改后 mean 1608.75ms、P95 2457.86ms，修改前 mean 2556.22ms、P95 4598.42ms，HTTP 调用 40 对 39（其中 1 次是修复英文独立问题后新增的真实查询），没有明显延迟回归证据。单次采集仍不能替代长期监控。
