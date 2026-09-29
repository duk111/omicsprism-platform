# OmicsPrism 多 Agent 拆分：编码步骤清单

前提：不改 `GraphState`、`CapabilityRegistry`、checkpoint 机制；只拆"模型协议层"——即
`AgentDecision`/`MainModelOutput` 的 action 空间、`MainModelContext` 的字段子集、
`readonly_openai_tool_definitions()` 的工具子集这三样东西。`analysis_node`（提交任务）
和 `result_qa_node`（证据查询）这两个确定性节点原则上不动。

按 6 个步骤推进，每步都能独立跑通、独立验证，不要合并提交。

---

## Step 0：建路由骨架，但先不接真实模型（纯管道验证）

**目标**：先把"三份 schema/context/tool 子集"这件事在类型层面立起来，图结构和现有行为完全不变，用来验证拆分之后每个 agent 的 schema 是不是真的合法、够窄。

- 在 `graph.py` 里新增：
  ```python
  class AgentRole(str, Enum):
      QA = "qa"
      ANALYSIS = "analysis"
      RESULT_QA = "result_qa"
  ```
- 新增三个更窄的 action union（可以先作为 `AgentDecision` 的子集校验，不用立刻改动
  `AgentDecision` 本体）：
  - `QaDecision.action: Literal["answer", "ask_user", "reroute"]`
  - `AnalysisDecision.action: Literal["inspect_dataset", "propose_plan", "run_analysis", "ask_user", "reroute"]`
  - `ResultDecision.action: Literal["query_result", "get_job", "grounded_answer", "ask_user", "reroute"]`
- `reroute` 是新增的 action，三个都要有——这是防止路由判断错误时模型硬编一个不属于自己
  职责的 action（对应 wiki Q14 那个"第二种被误判成 query_result"的问题）。
- `GraphState` 加一个 `reroute_count: int = Field(default=0, ge=0, le=2)`，防止
  route ↔ agent 之间死循环；超过上限直接走 `_MODEL_FALLBACK_QUESTION` 的 `ask_user`
  兜底，不再继续转发。

**验收**：三个新 union 各自能独立 `model_json_schema()`，跑一遍 vLLM 的 guided decoding
（哪怕先用假数据），确认没有 `oneOf`/`allOf` 冲突问题（对应 P3-2）。这一步不改
`main_node` 的实际执行路径。

---

## Step 1：拆 Context —— 每个 agent 一份窄投影

**位置**：`context.py::ContextAssembler`、`context.py::MainModelContext`

- 新增三个 context 模型（或者复用 `MainModelContext` 但拆出三个组装方法，二选一，
  建议直接拆三个类型，避免"字段存在但对某个 agent 无意义"这种隐性耦合）：
  - `QaModelContext`：`user_message` + `recent_messages` + `fact_index.dataset_roles`
    （只给"有哪些角色的数据存在"，不给 `metadata_fields`/`metadata_levels` 明细）
  - `AnalysisModelContext`：完整 `fact_index`（字段+水平，这是你要保留的推断能力）+
    `pending_analysis` 全量 + `decision_ledger`
  - `ResultQaModelContext`：`current_job`/`recent_jobs`/`focus.in_scope_job_ids` +
    `job_artifacts`，不需要 `metadata_fields`/`metadata_levels`
- `ContextAssembler` 加三个方法：`assemble_for_qa()`/`assemble_for_analysis()`/
  `assemble_for_result_qa()`，内部复用现有的 `_fact_index()`/`_decision_ledger()`/
  `_working_set()` 私有方法，只是按 agent 过滤要不要塞进最终对象。

**验收**：拿一条真实的 8 条历史消息 + 完整 metadata 的 thread，分别跑三个
`assemble_for_*()`，比较三份 context 序列化后的 token 数——`assemble_for_qa` 应该明显
小于现在的 `MainModelContext`。

---

## Step 2：拆模型调用入口 —— 按 role 切 system prompt / tools / schema

**位置**：`model.py::VllmGraphModel.__call__`（现在硬编码
`_GRAPH_MAIN_SYSTEM_PROMPT` 和 `readonly_openai_tool_definitions()`，行约 91/197-206）

- `__call__` 签名改成接受一个 `role: AgentRole` 参数（或者拆成
  `__call__(self, context, *, role)`），内部按 role 分支：
  - system prompt：把现在这一整段 `_GRAPH_MAIN_SYSTEM_PROMPT` 拆成
    `_QA_SYSTEM_PROMPT`/`_ANALYSIS_SYSTEM_PROMPT`/`_RESULT_QA_SYSTEM_PROMPT`
    三段，每段只保留该 agent 真正需要的指令（比如 analysis 的 prompt 要讲
    "如何用 metadata 字段推断 compare_field/scope"，qa 的 prompt 完全不需要
    这部分）
  - tools：`readonly_openai_tool_definitions(names={...})` 按 role 传不同的
    `names` 集合——`capabilities.py` 的 `openai_tool_definitions()` 已经支持
    `names` 过滤，这里直接复用，不用改 `capabilities.py`
  - output schema：`MainModelOutput.model_validate(...)` 改成按 role 校验对应的
    `QaModelOutput`/`AnalysisModelOutput`/`ResultQaModelOutput`（各自包一层
    `decision: QaDecision/AnalysisDecision/ResultDecision` + `answer`）

**验收**：三个 role 分别跑一次真实请求，确认返回的 `tools` 列表和用到的 schema
确实是各自的子集（可以在 trace 里加一条 `agent_role` 字段方便核对）。

---

## Step 3：路由层——规则优先，模型兜底

**位置**：新建 `agent/router.py`；复用 `model.py` 里现有的
`_is_explicit_jobs_listing`/`_is_explicit_job_status_request`/
`_is_explicit_analysis_request`（这几个函数目前的角色是"篡改模型决策"，
挪到这里之后角色变成"决定路由到哪个 agent"，函数体基本不用改，只改调用方式）

- `route(state: GraphState) -> AgentRole` 决策优先级：
  1. `state.pending_analysis is not None and state.pending_analysis.status == "active"`
     → 优先判断这条消息是不是明确的新知识问题/新结果查询（用现有关键词函数排除），
     排除不掉就直接给 `AgentRole.ANALYSIS`（因为大概率是在回答澄清问题）
  2. 消息命中 `_is_explicit_job_status_request`/`_is_explicit_jobs_listing` 且
     `state.current_job or state.recent_jobs` 非空 → `AgentRole.RESULT_QA`
  3. 消息命中 `_is_explicit_analysis_request` → `AgentRole.ANALYSIS`
  4. 都不命中 → 如果 `state.dataset_profiles` 和 `state.recent_jobs` 都为空，
     直接 `AgentRole.QA`（新对话大概率是闲聊/知识问题）；否则走一次极轻量的
     模型分类调用（见下面 Step 3.1）
- **Step 3.1（可选，先不做也行）**：`classify_agent_role(user_message, recent_messages)`——
  一次不带工具、不带完整 context 的极小模型调用，输出结构只有
  `{"role": "qa"|"analysis"|"result_qa", "confidence": float}`，prompt 很短，
  token 开销可以忽略。只在规则判断不出来的时候才调用这个。

**验收**：拿 wiki Q16 列的四个回归场景（"第二种"、包含"不要/不用"的其他问题、
先问知识问题再"继续刚才分析"、重新上传数据）过一遍 `route()` 函数的纯规则判断，
确认路由结果符合预期，不需要真的接模型也能先测这一层。

---

## Step 4：拆图结构 + 接入 reroute + Job continuation 直连

**位置**：`graph.py::build_agent_graph`、`nodes/main.py`（拆分后大部分逻辑挪到
`nodes/qa.py`/`nodes/analysis_agent.py`/`nodes/result_qa_agent.py` 三个新文件）、
`runtime.py::_run_continuation`

- 先把 `nodes/main.py::main_node` 里的 while 循环骨架（预算检查、两次重试、工具
  执行、重复调用兜底）抽成一个共享的内部 helper，签名类似：
  ```python
  def _run_agent_loop(
      role: AgentRole,
      model: MainDecisionModel,
      tool_executor: ToolExecutor | None,
      trace_recorder: TraceRecorder | None,
      context_builder: Callable[[GraphState], object],
      output_model: type[BaseModel],
      allowed_tools: set[ToolName],
  ) -> Callable[[GraphState], dict[str, object]]:
  ```
  三个新 node 文件各自调用这个 helper，传入自己的 context builder / output schema /
  工具子集，不用把预算、重试、重复调用这些通用逻辑复制三遍。
- **注意**：现有 `nodes/analysis.py` 里的 `analysis_node` 是"提交/确认"这一层
  确定性逻辑，跟这里要新建的 `nodes/analysis_agent.py`（LLM 决策循环）是两个不同
  的东西，命名上要明确区分，不要互相覆盖。`result_qa` 同理：新建
  `result_qa_agent_node`（LLM 循环）喂给现有 `result_qa_node`（证据查询，不动）。
- `build_agent_graph` 图结构改成：
  ```python
  builder.add_node("route", route_node)
  builder.add_node("qa_agent", qa_agent_node(...))
  builder.add_node("analysis_agent", analysis_agent_node(...))
  builder.add_node("result_qa_agent", result_qa_agent_node(...))
  builder.add_node("analysis", analysis_node(...))       # 不动
  builder.add_node("result_qa", result_qa_node(...))     # 不动

  builder.add_edge(START, "route")
  builder.add_conditional_edges("route", route_after_route, {
      "qa": "qa_agent", "analysis": "analysis_agent", "result_qa": "result_qa_agent",
  })
  builder.add_conditional_edges("qa_agent", ..., {"end": END, "route": "route"})
  builder.add_conditional_edges("analysis_agent", ..., {
      "analysis": "analysis", "end": END, "route": "route",
  })
  builder.add_conditional_edges("result_qa_agent", ..., {
      "result_qa": "result_qa", "end": END, "route": "route",
  })
  ```
- `reroute` 分支要先检查 `state.reroute_count`，超过 2 次直接终止到
  `_MODEL_FALLBACK_QUESTION` 的 `ask_user`，不再回 `route`。
- **`reroute` 不是异常分支，是预期用法之一**：用户在分析确认卡片阶段转头问别的
  问题、聊别的话题，这类"跑题"应该被 `analysis_agent`/`result_qa_agent` 自己识别
  出来并 `reroute` 到 `qa_agent`，`pending_analysis`/`current_job` 这些状态原样
  保留在 `GraphState` 里不动。因为路由是每一轮重新根据 `GraphState` 判断的（不是
  agent 循环内部维护的调用栈），用户聊完跑题话题之后只要不再命中"跑题"判断，
  `route()` 会自动把下一条消息重新带回原来的 agent——**不需要专门写一套"记住中断
  在哪一步"的恢复逻辑**，这是状态驱动路由本身自带的能力，实现时直接利用，不要
  另起一套栈。
- **Job completion continuation turn 要绕开路由分类，不能走自然语言判断**：
  `runtime.py::_run_continuation` 现在把 job 完成事件合成一条
  `"System Job event: {job_id} reached {status}."` 的 `user_message` 塞进图里，
  这不是自然语言，不应该走 `route()` 的关键词/小模型分类。改法：
  1. 给 `AgentTurnWorkItem`/`GraphState` 加一个显式字段
     `turn_origin: Literal["user", "job_continuation"] = "user"`，
     `_run_continuation` 构造 `state.model_copy(...)` 时把它设成
     `"job_continuation"`，不要靠识别 `"System Job event:"` 这个字符串前缀
     （字符串前缀判断本身就是又一次"用文本猜类型"的脆弱模式）。
  2. `route()` 第一步先查这个字段：`turn_origin == "job_continuation"` →
     直接给 `AgentRole.RESULT_QA`，跳过所有规则/分类判断。
  3. `_run_continuation` 里那句拼接文案（`f"System Job event: ... reached
     {event.status.value}."`）不能直接展示给用户，只作为喂给
     `result_qa_agent` 的结构化事实（job_id/status/error_code），最终话术由
     `result_qa_agent` 的模型生成给出（跟 Step 6 的 P2-1/P2-4 是同一个原则）。
  4. 不需要额外处理"job 跑的时候用户问别的问题会不会卡住"——`nodes/analysis.py`
     提交时已经把 `pending_analysis` 标成 `"consumed"`（不再是 `"active"`），
     job 本身在独立 worker/队列里跑，跟当前对话轮次处理完全解耦，这条链路现有
     架构已经支持，不用改。

**验收**：图1~图5 五个截图场景 + wiki Q16 四个场景，全部跑一遍完整链路（不只是
`route()` 函数），确认每条消息落在预期的 agent 节点上，`reroute` 只在故意构造的
边界场景（比如伪造一个错误路由）里触发，正常场景不应该触发 `reroute`；额外验证：
提交分析后立刻问一个无关知识问题，确认不阻塞、不误判成"还在确认分析"；用一个
合成的 `turn_origin="job_continuation"` 状态跑一遍，确认直接落在
`result_qa_agent`，不经过任何规则判断。

---

## Step 5：接入澄清参数解析子 Agent（wiki 设计落地 + 意图分类 + 阈值参数编辑）

**位置**：新建 `agent/clarification_resolver.py`，接入点在
`nodes/analysis_agent.py` 循环的最前面；参数规格来源
`param_resolver.py::DEGParams/DEMParams/GMAParams`

### 5.1 意图分类先行，不要直接进参数映射

`AGENT_CLARIFICATION_RESOLVER_DESIGN_ZH.md` 原设计只覆盖"匹配候选选项"这一种
情况，实测发现确认卡片阶段用户实际会有五种反应，需要先分类再分流：

```python
class ClarificationResolverOutput(BaseModel):
    intent: Literal["edit_params", "confirm", "cancel", "param_question", "unrelated"]
    matched_option_id: str | None = None
    proposal_patch: dict[str, AgentParamValue] = {}
    answer_text: str | None = None   # intent == "param_question" 时的解释文案
    confidence: float
    reason: str

class ClarificationResolverInput(BaseModel):
    user_reply: str
    pending_question: str | None
    options: list[ClarificationOption]        # {option_id, label}，contrast 相关
    param_spec: dict[str, ParamFieldSpec]      # 见 5.3，阈值参数相关
    recent_messages: list[RecentMessage]
```

五种 `intent` 的处理方式：

| intent | 处理方式 | 对 `pending_analysis` 的影响 |
|---|---|---|
| `edit_params` | `proposal_patch` 合并进 `pending_analysis`，重新走确定性校验（5.4） | contrast 字段变了要重新 `enumerate_contrasts`；纯阈值字段只需重新做一次 Pydantic 校验，不用重新枚举 |
| `confirm` | 用户打字确认（而不是点确认卡片的 Continue 按钮），等价于走原有提交路径 | 状态转 `consumed`，交给 `analysis_node` 提交 |
| `cancel` | 用户明确放弃这次分析 | 状态转终止态，不提交任务，回一句自然语言确认已取消 |
| `param_question` | 见 5.2，`answer_text` 直接由 analysis_agent 过模型生成给用户 | **原样保留 `active`**，不消耗、不清空 |
| `unrelated` | 输出 `reroute` 到 `qa_agent`（复用 Step 4 的 reroute 机制） | **原样保留 `active`**，靠状态驱动路由自动带回，不用额外维护恢复逻辑 |

### 5.2 `param_question`——不需要 reroute，不需要工具调用

用户问"padj_cutoff 是什么意思"这类问题，答案是固定的技术释义，跟具体数据集无关，
不需要绕到 `qa_agent`。跟 5.3 的参数规格一起维护一份人话解释（一个字段一句话），
`analysis_agent` 直接用这份解释过一次模型生成给出 `answer_text`，成本比 reroute 低，
`pending_analysis` 不受影响。

### 5.3 阈值参数规格——按 analysis_type 自动内省，不要手写三份

`DEGParams`/`DEMParams`/`GMAParams` 的字段集合、`ge`/`le` 约束已经是 Pydantic
`model_fields` 里的现成信息，规格表从这里自动生成，不要为三个类型手写三份文档
（避免重蹈 `min_replicates` 默认值四处硬编码、容易漂移的覆辙）：

```python
def build_param_spec(model: type[BaseModel], descriptions: dict[str, str]) -> dict[str, ParamFieldSpec]:
    ...  # 内省 model.model_fields 拿 ge/le/default，descriptions 只提供人话解释
```

三个类型的差异（这是 GMA 参数表单截图对比 DEG/DEM 表单截图看出来的）：

- **DEG**：contrast 字段（compare_field/tested_levels/reference_level/same_fields）
  + 阈值字段（padj_cutoff/log2fc_cutoff/min_total_count/min_replicates）
- **DEM**：同 DEG 的 contrast 字段 + 阈值字段（padj_cutoff/log2fc_cutoff/
  vip_cutoff/min_replicates/max_missing_fraction/impute_method）
- **GMA**：**没有 contrast 概念**——group.csv 本身固定 sample_id/group1/group2
  三列，两组由数据直接定义，不需要推断——`clarification_resolver` 面对 GMA 时
  `options`/`matched_option_id` 这条支路整个不触发，只处理阈值字段
  （fdr_cutoff/max_missing_fraction）

`ClarificationResolverInput.param_spec` 按当前 `pending_analysis.analysis_type`
传对应那一份，不要把三份都塞给模型。

### 5.4 校验层不变

`proposal_patch` 不管是 contrast 字段还是阈值字段，最终都要重新过一次
`DEGParams(**merged)`/`DEMParams(**merged)`/`GMAParams(**merged)` 的 Pydantic
构造（`ge`/`le` 约束在这里生效），校验失败带着具体原因打回 `ask_user`，不能因为
走了子 Agent 就跳过这一层——这是 wiki 设计文档强调的安全边界，子 Agent 的输出
永远不可信，只有过了确定性校验才能进 `resolve_analysis_request`/提交。

### 5.5 前置依赖

`enumerate_contrasts`/`_clarification_question` 生成候选时要给每个候选一个稳定的
`option_id`（wiki"后续实现要点"第一条），现在如果候选是靠列表顺序隐式对应的，
要先补上这个字段，不然 resolver 的 `matched_option_id` 没有稳定锚点。

### 5.6 "提交前用户重新上传数据"——已经实现，拆分时不用动

`runtime.py:308-316` 已经有这段逻辑：

```python
has_new_inputs = bool(turn_input.dataset_profiles)
...
if pending_analysis is not None:
    if has_new_inputs:
        pending_analysis = pending_analysis.model_copy(update={"status": "superseded"})
```

只要这一轮带新上传文件，现有 `pending_analysis` 会被标成 `superseded`，
`route()` 只认 `status == "active"`，`superseded` 之后的消息会被当成全新请求
正常路由，不会再被拉回澄清流程。`model.py:764` 也已经把
"consumed/superseded/expired 状态只作历史参考"写进了 system prompt。**这条不需要
拆分时额外处理，保持现状即可**——唯一可选的后续优化是按角色重叠判断要不要真的
作废（现在是"只要有新上传就作废，不管角色相不相关"），不紧急，可以先不做。

**验收**：
1. wiki Q16 的"第二种"场景——上传数据 → 模型问 contrast → 用户回复"第二种" →
   resolver 判 `intent=edit_params`，映射到对应 `option_id`，走
   `resolve_analysis_request` 确认流程，全程不进 `result_qa_agent`
2. 确认卡片阶段问"padj_cutoff 是什么意思" → `intent=param_question`，
   `pending_analysis` 保持 `active`，问完能接着改参数或确认
3. 确认卡片阶段突然聊别的话题 → `intent=unrelated` → reroute 到 `qa_agent`，
   `pending_analysis` 不受影响；跑题几轮后说"继续刚才的分析"，能正确被带回
   `analysis_agent` 且候选/参数没有丢失
4. 确认卡片阶段说"取消吧" → `intent=cancel`，`pending_analysis` 终止，不提交任务
5. 一句话同时改多个阈值参数（"把 min replicates 改成 1，log2FC 放宽到 0.5"）→
   `proposal_patch` 同时包含两个字段，只走 Pydantic 重新校验，不重新枚举 contrast
   候选
6. GMA 场景下确认卡片阶段说"fdr 改成 0.1" → 正确映射到 `fdr_cutoff`，且不触发
   任何 contrast 相关的候选枚举逻辑
7. 提交分析后立刻上传一份新数据 → `pending_analysis` 已经是 `consumed`（不是
   `active`），确认这个转场不会跟 5.6 的 `superseded` 逻辑冲突

---

## Step 6：清理与收尾

- 把 P2-1（`_list_jobs_response`/`_budget_question` 这类硬编码文案）的模型生成
  改造，限定在 `nodes/result_qa_agent.py`/`nodes/qa.py` 各自的职责范围内做，
  不再放在原来的公共 `nodes/main.py`。
- P2-4（`GroundedAnswer` 裸字段拼接兜底）同样收进 `result_qa_agent` 的范围。
- Job continuation 的完成/失败文案（Step 4 提到的 `_run_continuation` 拼接串）
  一并纳入这次模型生成改造，跟 P2-1/P2-4 是同一类问题，不单独算一条。
- P2-2（前端 `advisory`/`recommendation` block）**已决定直接删除**，不在多 Agent
  拆分里实现——`qa_agent` 不承担"主动给分析建议"的职责，只做问答，避免给模型
  增加不必要的输出负担。`MessageBlocks.tsx` 里 `case "advisory"`/
  `case "recommendation"` 两个渲染分支可以在这一批一并删掉。
- Step 5.3 的 `param_spec` 人话解释（`descriptions` 字典）跟阈值字段本身一样，
  按 analysis_type 维护一份，避免又出现"字段列表在代码里、解释文案在文档里"两处
  不同步的情况——建议直接和 `build_param_spec` 的 `descriptions` 参数放在同一个
  模块里维护。
- 确认三个 agent 的重复调用兜底（P0-2 的参数哈希比对）在拆分后依然生效——因为
  现在每个 agent 的工具集变小了（2-3 个），理论上误判概率进一步下降，但逻辑本身
  不需要改。
- 旧的单一 `main_node`/`MainModelOutput`/`AgentDecision`（9 action 版本）建议先用
  一个 feature flag（比如 `settings.py` 里加 `agent_multi_role_enabled: bool`）
  控制切换，跑一段时间确认三 agent 版本稳定后再删掉旧代码，方便出问题时快速回滚。

---

## 验证清单（每步都可以复用）

1. 图1~图5 原始截图场景（上传数据问能做什么分析 / 累积上传 / 连续追问火山图 /
   完整 DEG 确认流程 / "我有哪些任务"）
2. wiki Q16 四个场景（"第二种" / 含"不要"的跑题问题 / 知识问题打断后恢复分析 /
   重新上传数据使 pending 失效）
3. 故意构造一次路由误判（比如把 `route()` 硬编造错），确认 `reroute` 能在 2 次
   内收敛到 `ask_user` 兜底，不会死循环
4. 三个 agent 各自的 `model_json_schema()` 过一遍 vLLM guided decoding，确认没有
   `oneOf`/`allOf` 报错或降级成非严格模式
5. Step 5 补的六个场景：`edit_params`（含一次改多个阈值参数）/`param_question`/
   `unrelated`（跑题后能被带回）/`cancel`/GMA 场景只走阈值编辑不触发 contrast
   枚举/提交后立刻上传新数据不跟 `superseded` 逻辑冲突
6. 合成一条 `turn_origin="job_continuation"` 状态，确认直接落在 `result_qa_agent`
   且不经过任何规则/分类判断；完成文案和失败文案都过模型生成，不是原始拼接串

---

## 已经决定、不用再考虑的点

- **混杂检测（confound check）**：不做。提交前确认卡片 + 用户随时可改，这个
  机制本身已经是安全网，不需要代码或模型替用户做"要不要分层"的设计判断。
- **`min_replicates` 系统默认值**：维持 2，不改。
- **P2-2（`advisory`/`recommendation` block）**：直接删除，不在 `qa_agent` 里
  实现，避免增加模型输出负担。

## 还需要你确认的几个点

- **Step 3.1 的小模型分类要不要做**：如果规则判断（`_is_explicit_*` 三个函数
  加上 pending_analysis 状态）在你实际数据上命中率已经够高，可以先不接这次小
  模型调用，等看到误路由案例再加，避免过度设计。
- **`reroute_count` 上限设 2 是否合适**：如果你观察到正常场景里偶尔会有一次
  合理的 reroute（比如用户真的从分析话题跳到知识问题），上限设太低可能误伤，
  可以先设 2 观察一段时间再调。
- **QA agent 不给任何只读工具**：既然 P2-2 决定不做 advisory，`qa_agent` 目前
  设计成纯问答、不碰用户数据、不带工具即可，除非后续你想让它能结合用户数据回答
  "我这个数据能做什么分析"这类问题，再单独评估要不要给 `describe_metadata` 的
  粗粒度版本。
