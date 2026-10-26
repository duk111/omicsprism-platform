# PR9 阶段 A：意图路由与上下文审计

日期：2026-10-26（Asia/Shanghai）。基线 `33d4dc0`，审计开始时工作区干净。

## 当前调用链

`CopilotPage → agentApi.createTurn → agent/api.py:create_turn → AgentTurnInput/queue → AgentRuntime._run_start/_merge_turn_input → LangGraph route → qa_agent / analysis_agent / result_qa_agent → analysis / result_qa → checkpoint → product_store/SSE`。

1. `router.route` 先判断 continuation、术语定义、聊天中的结果特征、显著实体、pending clarification，再匹配结果/分析关键词；只有带 dataset profile 的部分模糊数据请求调用无工具 classifier。`focus_job_ids` 本身没有强制覆盖全部意图，但泛化的 result/padj/分析关键词会覆盖显式知识或操作请求。
2. 页面从 `?job_id=` 创建带 focus 的 thread；发送消息时也可传 focus。API 在入队前验证显式和继承 focus 的所有权；runtime 将显式 focus 合入 checkpoint。
3. `RunFocus.in_scope_job_ids`、`current_job`、`recent_jobs` 同时存在；Result QA 的模型看到 Job 引用和 artifact 名称，工具通过用户绑定 runtime、真实 Job 和 artifact schema 确定 DEG/DEM/GMA。聊天不授予访问权限。
4. 图默认 InMemorySaver，生产 bootstrap 注入 PostgresSaver；每轮 runtime 读取同 thread checkpoint，重置决策/工具观察/证据等临时字段，加载最多 8 条、每条 800 字符的聊天及摘要。不存在新记忆数据库需求。
5. Result QA 两个入口为 `tool_call(query_artifact)` 与 `query_result → result_qa`；最终经 PR7 GroundedAnswerPipeline/AnswerVerifier 校验，PR8 工具负责真实表头、实体、排序和数值过滤。
6. analysis_agent 的 pending clarification resolver 与 analysis 节点的 interrupt/ConfirmationResume 负责修改、取消、确认；普通表单不经此 router。本次不改变表单、执行器和参数模型。

## 已确认问题

- 结果规则先于操作规则：有历史 Job 时 `Run DEG with padj...`、修改含结果术语的待确认参数可进入 Result QA；`DEG 中 padj 表示什么` 也被 padj 误判为结果。
- runtime 改变 focus 时追加 recent_jobs，却不更新 current_job，也不清 resolved_entities/last_citation、旧聊天和 conversation memory；旧 Job 可持续影响新任务。
- 每轮清除 ToolResult/grounded_answer，没有 Job 绑定、可重查的上一轮查询集合。最近聊天只保存模型文本，无法安全确定“其中”的五个基因。
- 多 Job 的“继续分析刚才那个结果”可被通用“分析”规则送进 Analysis；缺少确定性、唯一 Job 检查。
- 图已有 visited_roles（最多 3 个且禁止重复）和模型/工具预算，并非无限 reroute；仍存在误路由后额外模型调用。classifier 调用未计入 step budget，需要补齐。
- 前端仅初始化时消费 job_id；已挂载 Copilot 后 URL 切换 Job 依赖消息携带 focus，需加极小的切换回归/处理。
- 已有权限、证据、确认机制有效，应复用，不重写。删除/失权仍须在实际读取时重新校验。

## 最小实施方案

1. 原 router 调整优先级：明确操作/待确认计划修改优先；知识解释保持 QA；结果和代词追问使用有效 Job/查询选择；不唯一时在 route 节点直接简短澄清，不创建 Job。保留 classifier 作为既有不确定路径。
2. 仅扩展现有 State/Context：保存有限的、由真实查询证据产生的 Job+artifact+checksum+原查询+记录号/实体选择；不把模型回答当事实。追问重新执行原只读查询并验证 checksum/记录集合，再从实际证据比较。继续经过 PR7 校验。
3. runtime 切换 focus 时更新唯一 current_job，清除旧结果选择、引用和旧提示上下文；同 Job 保持选择。历史消息按最近 focus 切换边界截断，避免下一轮重新加载旧 Job 文本。API/工具继续校验实际所有权。
4. 复用现有确认/取消/修改流程，不改图结构、不新增 Agent/依赖/数据库。仅在必要时修复前端 URL focus 更新。

## 失败测试与评估计划

- 先录制原版 AgentEvalV2 CI 基线，再补中英文路由矩阵、同基因/五基因集合追问、Job 切换/多 Job/缺失/删除/无权限、待确认修改与取消测试，确认失败。
- 真实落盘 fixture + 原 capability/runtime/graph 验证多轮，故意污染助手历史和跨 Job 工具参数，确保数值与引用来自实际查询。
- 执行 PR7 evidence/grounding、PR8 DEG/DEM/GMA 查询、runtime、graph、confirmation 和现有 AgentEvalV2；补充路由与上下文场景的实际 grader，报告模型调用/reroute/延迟。
- CI Recorded/Scripted Model 的准确率仅代表确定性流程和边界验证；不代表真实模型语义质量或线上延迟。已有 PR8 报告记录 capability 4/6 的基线失败，重新执行核实，不更改断言掩盖。
