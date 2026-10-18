# Agent 分析链路三个问题排查手册

本文覆盖当前三个用户可见问题：

1. 补充 `compare_field` 后报 `analysis_type is required`；
2. 确认后一直显示加载，并提示 `Agent graph checkpoint is unavailable`；
3. 用户已经明确分析参数，Agent 仍返回参数不明确。

总体边界采用常见的 Agent 工程做法：LLM 负责自然语言理解和结构化候选；JSON Schema/Pydantic 约束输出；代码负责事实校验、状态转换、样本计数和 Job 提交。使用现有 LangGraph checkpoint、interrupt、typed schema 和 deterministic validator，不增加第二套会话状态。

## 一、补充参数后丢失 `analysis_type`

### 现象

第一轮请求能识别 DEG，并列出 metadata 字段；用户第二轮只回复：

```text
compare_field: treatment
```

系统返回：

```text
参数修改未通过校验：analysis_type is required
```

### 根因

第一轮分析节点可能已经在局部变量中补全 `analysis_type=DEG`，但创建 `PendingAnalysisClarification` 时保存的仍是原始 proposal。于是 checkpoint 中出现不一致：

```json
{
  "pending_analysis": {
    "analysis_type": "DEG",
    "proposal": {"analysis_type": null}
  }
}
```

第二轮澄清 resolver 只返回增量 patch：

```json
{"intent":"edit_params","proposal_patch":{"compare_field":"treatment"}}
```

patch 以不完整 proposal 为 base 合并，最终 proposal 没有 `analysis_type`，于是 `_validate_patch_shape()` 拒绝它。

这是 pending 状态持久化和增量 patch 合并错误，不是 `treatment` 非法。

### 排查方法

按同一 `thread_id`/`turn_id` 检查：

1. 第一轮的 `decision.analysis_type`、`decision.proposal.analysis_type`、`resolved_request.analysis_type`；
2. 第一轮完成后的 `pending_analysis.analysis_type` 和 `pending_analysis.proposal.analysis_type`；
3. 第二轮 resolver 的 `intent`、`proposal_patch`、`matched_option_id`；
4. `_proposal_for_pending()` 合并前后的 `base.analysis_type` 和最终 `proposal.analysis_type`；
5. `param_specs_for_analysis(pending.analysis_type)` 是否仍为 DEG 参数规格。

### 标准解决方法

保持一个简单不变量：

```text
pending_analysis.analysis_type == pending_analysis.proposal.analysis_type
```

第一轮缺参数时保存完整 candidate proposal；第二轮只保存用户本轮 patch，但合并时始终以完整 candidate 为 base。若 proposal 的类型为空，则使用 `pending_analysis.analysis_type` 补齐；补齐后仍必须重新执行 `resolve_analysis_request()` 和 `validate_analysis_request()`。

澄清 resolver 只做自然语言到 typed patch 的映射，不读取 CSV、不计算样本、不修改 checkpoint、不提交 Job。

### 验收标准

```text
用户：帮我做差异基因分析
Agent：请选择比较字段
用户：treatment
```

第二轮后的 candidate 必须仍包含：

```json
{"analysis_type":"DEG","compare_field":"treatment"}
```

然后继续询问真正缺失的 tested/reference/scope。

## 二、确认后一直加载，提示 checkpoint unavailable

### 现象

前端显示：

```text
Working on your request
Agent graph checkpoint is unavailable
```

### 根因

确认消息是异步 turn：

```text
前端 -> API -> Redis Agent queue -> Agent Runtime -> LangGraph checkpoint -> 前端恢复
```

前端 `recover()` 使用 `Promise.all()` 同时读取 thread、turns、messages、pending-interrupt、job-waits 等资源。后端读取 `graph.get_state()` 或解析 `GraphState` 的任何异常，当前都被包装成 `Agent graph checkpoint is unavailable`。一个 checkpoint 请求失败后，前端不会应用其他已经成功读取的 turn/message 状态，于是旧的 running 状态一直保留，表现为永久 loading。

这条错误不能直接证明 GMA 分析失败。真实原因可能是：

- API 和 Runtime 使用了不同 checkpoint PostgreSQL DSN；
- Redis queue 没有被 Runtime 消费；
- checkpoint 连接、权限或连接池异常；
- checkpoint 中的状态无法按当前 schema 反序列化；
- GMA 参数、PendingPlan 或 interrupt payload 不兼容；
- Runtime 已失败，但前端没有读到最终 failed turn。

### 排查方法

用同一 `thread_id`、确认对应 `turn_id` 和时间窗口检查：

```text
GET /api/agent/threads/{thread_id}
GET /api/agent/threads/{thread_id}/turns
GET /api/agent/threads/{thread_id}/pending-interrupt
GET /api/agent/threads/{thread_id}/messages
```

结果判断：

| 结果 | 判断 |
|---|---|
| turn=failed | Runtime 已失败，查 `failure_code` 和 Runtime 日志 |
| turn=running 且有 interrupt | 图正在等待下一步，前端恢复有问题 |
| turn=running 且无 interrupt | Runtime、队列或 checkpoint 可能卡住 |
| thread/pending-interrupt 返回 409 | checkpoint 读取或 GraphState 解析失败 |
| turns/messages 正常、thread 失败 | 不应让前端丢弃可用状态 |

Runtime 日志按 `turn_id` 检查：

```text
agent.turn.started
agent.turn.processed
agent.turn.failed
agent.turn.timeout
agent.model.rejected
```

同时核对 API 和 Runtime 的 checkpoint DSN、Redis URL、queue 名称、thread_id 和代码版本。确认 Redis 中的 turn 是否被消费，以及 checkpoint 是否存在对应 thread。

### 标准解决方法

不增加新的会话状态系统：

1. 后端区分 `agent_checkpoint_unavailable` 和 `agent_checkpoint_invalid`，不要统一吞成一个错误；
2. 前端恢复使用部分成功策略，checkpoint 读取失败时仍应用 turns/messages 中可用的终态；
3. queued/running turn 设置可见超时和停止/重试入口，禁止永久 loading；
4. Runtime 所有异常都必须持久化为 failed turn，不能只写日志；
5. GMA/DEG/DEM checkpoint payload 使用稳定 schema，升级不兼容时明确失败或迁移。

### 验收标准

- Runtime 成功：前端显示 assistant message 或下一步 interrupt；
- Runtime 异常：turn 进入 failed，前端停止 loading；
- checkpoint 暂时不可用：显示明确错误码，并保留可读的 turn/message 状态；
- GMA 确认流程明确区分“等待 interrupt”和“Job 已提交”。

## 三、用户已明确参数，Agent 仍返回参数不明确

### 现象

用户输入：

```text
可以做差异基因分析吗，要求对比同一 line 和 timepoint 下的 treatment 列的 salt 和 control
```

Agent 却返回：

```text
需要选择包含实验分组的 metadata 列
可选项：compare_field: line, treatment, timepoint, replicate
```

### 根因

当前 prompt 只是指导模型“从显式语言推断参数”，并没有强制模型把已明确事实写入 `AnalysisProposal`。由于 proposal 字段可选，模型可以合法返回：

```json
{
  "decision": {
    "action": "propose_plan",
    "analysis_type": "DEG",
    "proposal": {"analysis_type": "DEG"}
  }
}
```

此时确定性 resolver 看不到 `treatment/salt/control`，只能从 metadata 枚举所有字段，于是列出 `line/treatment/timepoint/replicate`。

另外，“同一 line 和 timepoint 下”通常表示按 `line × timepoint` 分层，但没有给出具体值时不能静默解释成 fixed filter，应展示推断并让用户确认。

### 排查方法

1. 从 trace 检查实际 `prompt_version`、`prompt_hash`、model/provider，确认 Runtime 已使用当前 prompt；
2. 在受控调试环境检查模型 structured output：

   ```text
   decision.action
   decision.analysis_type
   decision.proposal.analysis_type
   decision.proposal.compare_field
   decision.proposal.tested_level
   decision.proposal.reference_level
   decision.proposal.scope
   ```

3. 检查 `AnalysisModelContext` 是否包含 `user_message`、`analysis_catalog`、metadata fields/levels、recent messages、pending analysis 和 decision ledger；
4. 在 `resolve_analysis_request()` 前记录 `llm_proposal`、`prior_params` 和 `user_message`；
5. 如果 proposal 已有 treatment/salt/control 但 resolver 仍列出全部字段，再排查参数合并和 metadata resolver；如果 proposal 本身为空，问题在模型输出或模型边界。

### 标准解决方法

使用结构化输出优先，而不是继续堆关键词：

1. prompt 明确要求：用户明确说出的分析类型、比较字段、测试组和参考组必须写入 proposal；不得用 `ask_user` 替代已经明确的字段；只有无法判断时才留空；
2. structured output 后做一次轻量完整性检查：如果用户文本中的字段和值存在于 `fact_index`，但 proposal 对应字段为空，则进行一次结构化重试或返回基于事实的澄清；
3. 使用现有 `ScopeSpec` 表达范围：

   ```json
   {
     "compare_field": "treatment",
     "tested_level": "salt",
     "reference_level": "control",
     "scope": {
       "mode": "stratified",
       "blocking_fields": ["line", "timepoint"]
     }
   }
   ```

   只有用户给出具体 line/timepoint 值时才使用 `fixed_filters`；
4. proposal 仍必须经过 resolver、validator、样本计数和用户确认；
5. 关键词只保留为模型不可用或 structured output 无法解析时的低优先级 fallback，不能覆盖模型已经返回的结构化字段。

### 验收标准

给定 metadata 包含相关字段和值时，上述请求应生成至少包含以下内容的 candidate：

```json
{
  "analysis_type": "DEG",
  "compare_field": "treatment",
  "tested_level": "salt",
  "reference_level": "control",
  "scope": {
    "mode": "stratified",
    "blocking_fields": ["line", "timepoint"]
  }
}
```

如果字段、值或样本数不合法，系统应说明确定性事实，而不是重新列出全部 compare_field 候选。

## 四、实施顺序与统一验收

建议按依赖顺序处理：

1. 修复 pending candidate 不变量，补充多轮澄清回归测试；
2. 修复 checkpoint 错误分类、前端部分恢复和永久 loading；
3. 收紧结构化 proposal 抽取，增加显式事实完整性检查；
4. 再运行端到端测试，避免用关键词继续掩盖状态和 schema 问题。

统一验收：

| 场景 | 预期 |
|---|---|
| 首轮缺比较字段 | 保存完整 `analysis_type`，只询问缺失字段 |
| 下一轮只补字段 | 保留原类型，继续校验真实缺失参数 |
| 用户确认 | turn 进入 interrupt、completed 或 failed，不永久 loading |
| checkpoint 异常 | 明确错误码，保留可读状态，支持恢复/停止 |
| 用户已说 treatment/salt/control | proposal 包含完整对比候选 |
| “同一 line 和 timepoint 下” | 展示 stratified 推断并请求确认 |

该方案使用 Structured Outputs/JSON Schema、单一 `AnalysisSpecRegistry`、确定性 validator、LangGraph durable checkpoint/interrupt 和幂等 Job 提交，足以解决这三个问题，不需要新增独立 Agent 状态库或重复规则体系。
