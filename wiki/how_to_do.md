# OmicsPrism 多 Agent 设计基线

本文总结 Step0-5 后真实场景暴露的问题，并定义后续路由、Agent action、工具调用、失败恢复和验收规则。

本文的目标不是增加更多控制层，而是收敛为一条权威链路，优先复用现有 LangGraph checkpoint/interrupt、GraphState、role schema、工具 allowlist 和确定性 validator。

本文描述设计目标和实施边界，不代表所有目标行为已经实现。

## 1. 设计目标与非目标

### 1.1 设计目标

- 用户的自然语言请求能进入正确的 role，或明确返回无法判断/不支持。
- Agent 只能基于其职责范围内的事实作决定，不能覆盖 ownership、checksum、确认计划或执行校验。
- 工具调用必须有完整的 schema、allowlist、执行和 observation replay 链路。
- 一轮失败不会破坏会话中的持久状态，active analysis clarification 可以继续恢复。
- 系统失败、业务澄清和能力不支持使用不同的结构化结果。
- 同一件事只有一个权威来源，不在 router、Agent prompt、context assembler 和 validator 中重复维护规则。

### 1.2 非目标

- 不建立独立于 GraphState 的第二套会话状态栈。
- 不让 LLM 直接决定 Job ownership、数据 checksum、参数合法性或 Job 提交结果。
- 不要求 LLM 覆盖所有人类表达；低置信请求可以拒识。
- 不默认增加 response composer。自然语言包装优先使用目标 Agent 或确定性文案。

## 2. 已确认的问题

### 2.1 `ask_user` 掩盖真实错误

当前用户可能只看到“我暂时无法可靠判断你的意图”，但真实原因可能是：

- role schema 拒绝模型输出；
- role-specific native tool call 被拒绝，或工具执行失败；
- reroute target 缺失、非法或已经访问过；
- 数据、参数、ownership 或 checksum 校验失败；
- 模型服务不可用或响应格式不合法。

这些情况必须产生稳定的结构化 outcome/failure code，不能全部伪装成普通追问。

### 2.2 关键词路由无法覆盖自然语言

“这两个数据能做什么”“可以做差异分析吗”“帮我看看这些数据”“差异基因有哪些”“帮我分析一下差异基因”可能分别表示能力评估、分析计划、结果查询或知识解释。继续添加关键词只能增加局部覆盖，不能解决语义重叠。

### 2.3 checkpoint 完整保存不等于 Agent 看到全部字段

PostgreSQL checkpoint 保存完整 `GraphState`，但 Agent 收到的是 `ContextAssembler` 的角色投影：

- checkpoint 有字段，不代表当前 role 可以看到；
- 每个 role 至少需要当前 `user_message` 和必要的 `recent_messages`；
- metadata、Job、artifact 按职责过滤；
- correlation id 只用于 trace，不进入模型 prompt；
- tool observations 必须能在同一 role 的下一轮模型调用中 replay。

### 2.4 action 空间与用户语义不匹配

“可以做差异分析吗”可能只是能力咨询。如果 Analysis schema 只允许执行型 action，模型返回解释性内容时会被拒绝并退化到通用追问。

### 2.5 native tool call 必须走完整链路

```text
native tool_call
  -> role schema 校验
  -> allowed_tools 校验
  -> ToolExecutor
  -> ToolObservation
  -> 同一 role 的下一轮模型调用
  -> 最终 action 或结构化失败
```

不能因为是 role-specific call 就直接抛出 boundary error，也不能把工具失败无限 reroute 到其他 role。

## 3. 唯一控制链路

系统只保留以下五个阶段：

```text
持久状态与安全状态机
  -> 单一全局 route target
  -> 目标 role 的 typed action/tool loop
  -> 确定性校验与执行
  -> 结构化 outcome + 稳定用户文案
```

全局 router 只选择目标 role，不选择工具、完整参数或执行结果。目标 Agent 只在自己的职责范围内产生 typed action。确定性节点负责校验和提交，不能由模型绕过。

## 4. 第一层：持久状态与安全状态机

持久状态优先于自然语言分类，但 active pending 不应阻塞所有无关问题。推荐优先级如下：

1. confirmation interrupt resume 由确定性 confirmation flow 处理，不进入普通 router。
2. `turn_origin == "job_continuation"` 直接进入 `result_qa`。
3. 明确的取消、确认、继续分析或对当前 clarification 的参数回答进入 `analysis` 内部 resolver。
4. active `pending_analysis` 对普通消息默认进入 `analysis` resolver；明确的知识问题或结果查询可以直接进入对应 role，但 pending 必须保留。
5. 没有上述状态时进入普通全局路由。

`consumed`、`superseded`、`expired`、`cancelled` 的 pending 只作历史参考，不得自动恢复。

状态层也可能过时，但不能因为模型一句话就覆盖 Job ownership、confirmation plan、input fingerprint 或 checksum 等强事实。冲突必须由 reroute 或确定性校验处理。

### 4.1 clarification resolver

clarification resolver 是 `analysis_agent` 内部子流程，不是公共 graph node，也不是 `ToolName`：

```text
route -> analysis_agent
          -> pending_analysis.active ? clarification resolver : analysis decision loop
```

它只处理 `edit_params`、`confirm`、`cancel`、`param_question` 和 `unrelated`。它不读取原始 CSV、不提交 Job、不查询结果。参数修改之后仍必须经过 `resolve_analysis_request` 和确定性验证。

默认复用主 Agent 模型；独立模型是可选优化，不改变功能语义：

```text
OMICS_PRISM_CLARIFICATION_MODEL_URL
OMICS_PRISM_CLARIFICATION_MODEL_NAME
OMICS_PRISM_CLARIFICATION_MODEL_API_KEY
```

## 5. 第二层：单一全局路由结果

全局路由只输出一个目标，不维护全局 `intent + operation` 平行协议：

```python
class RouteDecision(BaseModel):
    target: Literal[
        "qa", "analysis", "result_qa", "ambiguous", "unsupported"
    ]
    source: Literal["state", "rule", "classifier", "reroute"]
    reason: str
```

`RouteDecision` 是当前 turn 的控制结果；如果持久化，必须作为 per-turn 字段在新 turn 清空。`reason` 只用于 trace、debug 和评估，不作为事实来源。

路由来源按顺序排列：

1. 持久状态和安全状态机；
2. 高置信确定性规则；
3. 无工具、短上下文的轻量分类器。

轻量分类器只负责选择 `qa`、`analysis`、`result_qa`，或在低置信时返回 `ambiguous`。`unsupported` 只应在确定性能力边界明确不支持时产生，不应依赖模型猜测。

分类器的置信度只用于拒识阈值和离线评估，不能直接当作模型自报的可信事实。阈值必须由中文和英文回归集校准，并记录误路由率、拒识率和误拒识率。

## 6. 第三层：目标 Agent 的 typed action

`action` 是目标 Agent 的语义决定；`tool` 是获取事实的中间机制：

```text
action=tool_call + tool + arguments
  -> ToolExecutor
  -> ToolObservation
  -> 同一 Agent 下一轮模型调用
  -> 最终 action
```

建议的最小 action 集合：

QA：`answer`、`ask_user`、`reroute`。

Analysis：`capability_query`、`inspect_dataset`、`propose_plan`、`run_analysis`、`tool_call`、`ask_user`、`reroute`。

Result QA：`query_result`、`get_job`、`grounded_answer`、`tool_call`、`ask_user`、`reroute`。

`capability_query` 是唯一的能力咨询 action。能力事实读取和自然语言组织属于该 action 的内部步骤，不再拆成 `assess_capability` 和 `explain_capability` 两个平行 action。

`run_analysis` 只表示进入确定性校验/提交路径，不表示模型可以直接提交 Job。提交仍必须经过参数解析、数据加载、ownership、input fingerprint、idempotency 和最终执行校验。

### 6.1 能力评估与可运行性

能力评估必须使用同一事实来源：

```text
AnalysisSpecRegistry
  -> 输入角色能力清单
  -> 当前数据角色缺项

resolve_analysis_request + validator
  -> 参数、对齐、contrast、样本和 checksum 校验
  -> 是否可进入提交路径
```

必须区分：

- **能力支持**：理论上该分析需要哪些输入角色；
- **当前可运行**：本次数据和参数已经通过完整确定性校验。

`ContextAssembler` 不应复制一套 DEG/DEM/GMA requirements。`fact_index.analysis_capabilities` 应从 `AnalysisSpecRegistry` 派生，并统一 `metabs`/`metabolome` 等角色别名。模型只能解释确定性结果，不能自行声称分析已经可运行。

能力咨询的推荐链路：

```text
RouteDecision(target=analysis)
  -> capability_query
  -> deterministic capability evaluator
  -> bounded capability report
```

该模式不读取无关完整 metadata、不提交 Job、不查询结果。

## 7. 工具调用边界

每个 role 必须有独立的 allowed-tools allowlist。工具调用统一遵守：

1. 解析 native tool call 或 typed `tool_call`；
2. 校验 role schema 和工具 allowlist；
3. 校验参数边界和当前用户 ownership；
4. 调用 `ToolExecutor`；
5. 保存 bounded `ToolObservation`；
6. 将 observation replay 给同一 role 的下一轮模型；
7. 达到预算、重复调用或不可恢复错误时产生结构化终态。

schema 错误、工具拒绝、工具执行失败、数据校验失败不应通过无限 reroute 解决。瞬时工具错误可以在明确的 retry budget 内重试；重试耗尽后保留原始失败事实。

## 8. reroute 状态机

合法转交方向：

```text
QA -> analysis / result_qa
Analysis -> qa / result_qa
Result QA -> qa / analysis
```

`reroute_to` 必填，不能等于当前 role，也不能指向本 turn 已访问过的 role。

每个用户 turn 只维护一个路由历史：

```python
visited_roles: list[AgentRole]
```

进入第一个 role 时记录该 role。转交到新 role 前先检查它不在 `visited_roles` 中，成功转交后追加。最大路径为 `A -> B -> C -> terminal outcome`。

不再同时维护 `visited_roles` 和独立的 `reroute_hops`。hop 可由 `len(visited_roles) - 1` 推导。新用户 turn 必须清空路由历史；当前实现中的 `reroute_count` 直接删除。

状态层或规则层判断错误时，当前 role 可以显式 reroute。schema、工具、数据和 ownership 错误必须进入对应 failure outcome，而不是转交给另一个 role 猜测。

## 9. 结构化 outcome 与稳定文案

用户可见结果至少分为以下类型：

```text
completed       已完成回答或操作
needs_input     需要用户补充参数/选择
unsupported     能力边界明确不支持
unresolved      无法可靠判断或路由耗尽
failed          系统或依赖失败
```

建议内部统一结构：

```json
{
  "outcome": "unresolved",
  "failure_code": "route_exhausted",
  "attempted_roles": ["analysis", "qa", "result_qa"],
  "response_text": "我还不能可靠判断这条请求属于哪类操作。你可以说：解释概念、评估数据能力、开始分析，或查询已有结果。"
}
```

建议稳定 failure code：

```text
route_ambiguous
route_exhausted
unsupported_request
role_schema_validation_failed
tool_call_rejected
tool_execution_failed
missing_analysis_parameter
ownership_validation_failed
checksum_mismatch
model_unavailable
```

`missing_analysis_parameter` 通常对应 `needs_input`，不是 runtime failure。`unsupported_request` 通常对应 `unsupported`，也不是服务崩溃。只有模型、工具、数据库或图执行等系统故障才应让 turn 进入 `failed`。

内部 trace 可以记录更细的依赖错误；对外 API 使用稳定 code，避免把 Python exception 名称或底层服务细节暴露给用户。

### 9.1 错误映射（冻结）

| 事实 | `outcome` | `failure_code` | `AgentTurnStatus` | `retryable` |
|---|---|---|---|---|
| 缺少必需分析参数/用户需要选择 | `needs_input` | `missing_analysis_parameter` | `completed` | `false` |
| 分析类型或输入角色明确不支持 | `unsupported` | `unsupported_request` | `completed` | `false` |
| 分类器低置信或三 role 路由耗尽 | `unresolved` | `route_ambiguous` / `route_exhausted` | `completed` | `false` |
| role schema 无法接受模型输出 | `failed` | `role_schema_validation_failed` | `failed` | `false` |
| 工具不在 allowlist 或参数被拒绝 | `failed` | `tool_call_rejected` | `failed` | `false` |
| 工具执行失败且重试已耗尽 | `failed` | `tool_execution_failed` | `failed` | 按依赖错误决定 |
| 模型服务不可用/响应超时 | `failed` | `model_unavailable` | `failed` | 按 provider 状态决定 |
| ownership 或 checksum 校验失败 | `failed` | `ownership_validation_failed` / `checksum_mismatch` | `failed` | `false` |

`AgentErrorBlock` 只承载表中的 `failure_code`、稳定用户文案和 `retryable`，不承载内部异常类型。需要用户补充信息时使用 `needs_input`，不得把普通业务澄清标记为 `failed`。

公开 turn response 必须直接返回 `outcome`、`failure_code` 和 `attempted_roles`，不得把这些字段隐含在 response text 中。

暂不默认增加 LLM response composer。稳定文案优先由 outcome/code 的确定性映射生成。未来引入 composer 时，必须保证它不能修改 outcome 或 failure code、编造事实、重新决定路由或启动新工具调用；composer 失败时回退到确定性文案。

### 9.2 公开契约（冻结）

`outcome`、`failure_code` 和 `attempted_roles` 进入 API，作为当前 Agent 链路的唯一公开结果契约：

```python
class AgentOutcome(str, Enum):
    COMPLETED = "completed"
    NEEDS_INPUT = "needs_input"
    UNSUPPORTED = "unsupported"
    UNRESOLVED = "unresolved"
    FAILED = "failed"

class AgentTurnResponse:
    status: AgentTurnStatus                 # 传输/执行状态，保持现有含义
    outcome: AgentOutcome | None            # 仅终态填写
    failure_code: str | None                # 业务或系统失败事实
    attempted_roles: list[AgentRole]        # 默认为 []，最多 3 个
```

契约规则：

- `queued`、`running` 时 `outcome`、`failure_code` 可以为空；
- `completed` 时 `outcome` 必须是 `completed`、`needs_input`、`unsupported` 或 `unresolved`；
- `failed` 时 `outcome` 必须是 `failed`，并且必须有 `failure_code`；
- `attempted_roles` 只记录本 turn 实际进入过的 role，按访问顺序去重；
- `AgentErrorBlock.code` 使用同一个 `failure_code`，`user_message` 使用稳定文案，`retryable` 由确定性映射给出。

`AgentTurnStatus` 仍表示队列、运行、完成、失败或取消，不与 `outcome` 合并。取消由现有 `status=cancelled` 表示，不伪装成 `failed`。

数据库和 trace 直接使用 `outcome`、`failure_code`、`attempted_roles` 的当前字段定义。

## 10. 会话恢复与 per-turn 边界

一轮失败不等于会话失败：

- PostgreSQL checkpoint 继续保存完整 `GraphState`；
- 新用户 turn 重置 `decision`、response、tool observations、route history 和 step budget；
- active `pending_analysis` 在 unrelated 问题后保留，用户说“继续刚才分析”时重新进入 analysis；
- `cancelled`、`consumed`、`superseded`、`expired` 不自动恢复；
- job continuation 通过 `turn_origin="job_continuation"` 独立进入 result QA；
- tool observations 属于当前 turn，不应污染下一轮普通用户请求。

确认计划、input fingerprint、Job ownership 和 idempotency key 属于持久安全事实，不能因为新一轮模型输出而被覆盖。

### 10.1 路由状态模型（冻结）

`GraphState` 增加当前 turn 专属字段：

```python
visited_roles: list[AgentRole] = []  # 最多 3 个，按访问顺序去重
```

不再把 `reroute_count` 作为权威字段。当前链路只使用 `visited_roles`；旧 checkpoint 不属于新协议，清理后再使用当前 schema。

状态继承规则：

| 场景 | 继承 | 清理/覆盖 |
|---|---|---|
| 普通新用户 turn | ownership-bound dataset、current/recent Job、active pending（若未过期且 input bundle 未变） | `decision`、response、tool observations、step budget、`visited_roles` |
| active pending + 无关问题 | active pending 保留 | 当前 turn 的 route/action/tool 临时字段清空；允许进入 `qa` 或 `result_qa` |
| confirmation resume | checkpoint 中的 plan、fingerprint、确认 payload | 不经过普通 router；confirmation flow 自己完成状态转换 |
| job continuation | Job continuation fact、ownership-bound Job/artifact | `turn_origin=job_continuation`，`visited_roles=[result_qa]`，不恢复普通 pending 流程 |
| pending 为 consumed/superseded/expired/cancelled | 仅保留历史记录 | 不自动恢复，不作为 active route 条件 |

进入首个 role 时追加到 `visited_roles`。reroute 前检查目标 role 不在列表中且列表长度小于 3；成功转交后追加。重复 role 或超过上限直接生成 `unresolved/route_exhausted`，不再回到 router。

## 11. 能力咨询协议（冻结）

`capability_query` 是 Analysis role 唯一的能力咨询 action。它不执行分析、不查询结果，也不要求模型生成完整分析参数。

### 11.1 输入

模型输出只允许一个可选的分析类型：

```python
class CapabilityQueryInput(BaseModel):
    analysis_type: Literal["DEG", "DEM", "GMA"] | None = None
```

- `analysis_type is None`：返回当前输入对所有已注册分析的能力摘要；
- 指定分析类型：只返回该分析的能力摘要；
- 数据集、用户和 ownership 不由模型填写，全部来自当前 `GraphState` 和已验证的 dataset profile；
- 不允许在该 action 中携带 Job id、原始文件内容、完整参数或提交意图。

### 11.2 确定性 evaluator

能力 evaluator 的唯一输入是 `AnalysisSpecRegistry` 和当前数据的规范化角色集合：

```python
class CapabilityItem(BaseModel):
    analysis_type: Literal["DEG", "DEM", "GMA"]
    supported: bool = True
    present_roles: list[str]
    missing_roles: list[str]
    next_step: Literal["ready_for_parameter_resolution", "missing_input_role"]

class CapabilityReport(BaseModel):
    items: list[CapabilityItem]
```

`next_step=ready_for_parameter_resolution` 只表示输入角色满足 spec，不表示参数、contrast、样本对齐或 checksum 已通过执行校验。是否可提交必须继续经过 `resolve_analysis_request`、`validate_analysis_request` 和最终 ownership/fingerprint 校验。

### 11.3 规范角色名和别名

`AnalysisSpecRegistry` 负责角色规范化、别名和分析输入规则，其他模块不得复制 requirements：

| 规范角色 | 输入别名 | 适用说明 |
|---|---|---|
| `counts` | 无 | DEG 输入矩阵 |
| `metabolome` | `metabs` | DEM/GMA 代谢组矩阵 |
| `transcriptome` | 无 | GMA 转录组矩阵 |
| `metadata` | 无 | DEG/DEM 元数据 |
| `group` | 无 | GMA 分组输入 |

dataset profile 入口可以继续接受 `metabs`，但进入 evaluator、`ContextAssembler`、validator 和报告前必须规范化为 `metabolome`。新增分析类型只能通过 registry 注册输入角色和参数规则，不能在 router 或 context assembler 增加硬编码分支。

### 11.4 输出与文案

evaluator 先返回 `CapabilityReport`，再由 Analysis role 组织用户语言。确定性文案必须明确“缺少输入角色”和“尚需参数解析”的区别；不得声称分析已运行、已有结果或已经提交 Job。

## 12. 历史实现清理原则

当前完整 Agent 链路定版后，不保留旧版本代码作为兼容层或运行时回退。实现前先盘点引用关系，确认当前入口和测试覆盖，再删除不属于当前链路的历史实现。

清理范围包括：

- 旧的 `AgentDecision`/main output 适配器、重复 role schema 和旧路由分支；
- 旧 fixture、legacy eval grader、过时的 recorded response 和旧 action 名称；
- 仅为旧客户端保留的 API 字段、错误码映射和前端兜底读取；
- 不再被当前 runtime、worker、API 或前端读取的数据库字段、trace 字段和迁移分支；
- `reroute_count`、重复的能力 requirements、旧 checkpoint 读取路径和无效的 fallback 分支。

清理规则：

1. 当前目标 schema、API、数据库字段、前端类型和 fixture 只保留一套定义。
2. 删除旧代码后，所有调用方直接切换到当前契约；不增加 adapter、双写、双读或 legacy feature flag。
3. 旧代码删除后运行完整测试、类型检查和静态引用扫描，确保不存在死引用和隐式 fallback。
4. 数据库清理使用明确的删除 migration；如果历史数据不再需要，直接删除旧字段，不为它们新增兼容读取。
5. 发现仍有调用方依赖旧接口时，先修改调用方和测试，再删除旧接口；不以保留旧接口代替重构。

输入别名只允许存在于当前业务输入规范化中，例如 `metabs -> metabolome`；它不扩展到 API、GraphState 或 action。

## 13. 可复用的成熟机制

- **LangGraph checkpoint/interrupt**：持久状态、确认暂停和恢复；
- **Pydantic/JSON Schema**：typed action、工具参数、跨字段约束和边界拒绝；
- **OpenAI-compatible tool calling**：native tool call 与最终 action 分离；
- **AnalysisSpecRegistry + validator**：输入能力和可运行条件的单一事实来源；
- **Result/Either 风格**：结构化成功、需要输入、不支持、未解析和失败结果；
- **Selective classification**：低置信拒识，而不是强行猜测；
- **Property-based/contract testing**：验证 allowlist、ownership、checksum、reroute 上限和状态恢复不变量。

不要再建立与这些机制重复的平行意图、状态或文案决策栈。

## 14. 验收场景

| 用户请求/状态 | 预期路径 | 关键断言 |
|---|---|---|
| “这两个数据能做什么” | `analysis -> capability_query` | 只解释确定性能力，不提交 Job |
| “可以做差异分析吗” | `analysis -> capability_query` | 区分输入角色缺项和可运行性 |
| “帮我分析一下差异基因” | `analysis -> propose_plan` | 缺参数进入 `needs_input`，不伪造执行结果 |
| active pending + “第二种” | `analysis -> clarification resolver` | 参数修改后重新走 resolver/validator |
| active pending + “什么是 FDR” | `qa`，pending 保留 | 不丢失原分析澄清 |
| 已有 Job + “差异基因有哪些” | `result_qa` | 只使用 ownership-bound artifact evidence |
| job continuation | `result_qa` | 不受普通关键词规则影响 |
| 非法 native tool call | 当前 role 的结构化失败 | 不直接 boundary crash，不无限 reroute |
| A -> B -> C 再次请求 A | terminal outcome | 本 turn 禁止重复访问 role |
| 新用户 turn | 初始 route | route history、tool observations、step budget 清空 |
| 模型不可用 | `failed/model_unavailable` | 使用稳定文案，不伪装成澄清问题 |

中文自然语言回归集至少覆盖能力咨询、知识解释、分析计划、结果查询、取消、确认、参数修改和无关追问。

## 15. 实施顺序

1. 冻结并实现公开 `outcome`、`failure_code`、`attempted_roles` 契约，删除旧 `error_code` 字段和读写路径。
2. 从 `AnalysisSpecRegistry` 派生规范角色、别名和 `analysis_capabilities`，移除 `ContextAssembler` 中的重复 requirements。
3. 固化状态优先级和 `visited_roles` 清理/继承规则，删除旧 `reroute_count` 和 checkpoint 回退路径。
4. 实现 `CapabilityQueryInput`、确定性 evaluator、`CapabilityReport` 和 `capability_query` action。
5. 引入单一 `RouteDecision`，分类器只负责 role/拒识，不负责 action、工具或参数。
6. 删除旧 `AgentDecision` 适配器、旧 fixture/grader、旧 API 字段、旧数据库字段和前端 fallback。
7. 补齐 role tool loop 的边界测试、native call replay、三 Agent reroute 和中文回归；当前工具循环能复用的部分不重复实现。
8. 每次部署验证 API、Agent Runtime、Worker 使用同一 commit，并执行验收场景、静态引用扫描和数据库清理 migration 测试。
