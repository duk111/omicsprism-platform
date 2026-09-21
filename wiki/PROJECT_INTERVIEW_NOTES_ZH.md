# OmicsPrism Agent 项目面经：工具调用与数据安全设计

## Q1：这个项目的 Agent 整体是怎么设计的？

我把系统拆成三层：模型协议层、工具能力层和业务图层。

- 模型协议层使用 OpenAI-compatible Chat Completions，模型通过原生 `tool_calls` 表达工具名称和参数。
- 工具能力层使用 `CapabilityRegistry` 统一管理工具 schema、参数校验、权限校验和执行函数。
- 业务图层使用 LangGraph 管理用户请求、工具循环、分析任务和最终回答。

模型只负责决定“调用什么工具”，后端负责决定“用户是否有权限调用，以及工具实际返回什么”。

## Q2：为什么选择原生 Tool Calling，而不是让模型输出普通 JSON？

传统做法通常是在 prompt 里描述工具格式，让模型输出一个自定义 JSON。这种方式需要应用自己解析、生成调用 ID、拼接 assistant 历史，协议约束比较脆弱。

我选择原生 Tool Calling，是因为工具定义、参数 JSON Schema、assistant `tool_calls` 和 `tool_call_id` 都是标准消息结构。模型返回的调用可以原样回放，工具结果也能精确对应到一次调用，减少协议适配代码和重复调用问题。

## Q3：为什么不用 Agent 框架的 `@tool` 作为核心？

`@tool` 只是某些框架提供的注册语法，不是工具协议。这个项目已经有 Pydantic request/response model 和 `CapabilityRegistry`，因此直接从类型模型生成 JSON Schema。

这样工具定义和执行边界来自同一份类型源，不会出现模型看到的参数和后端实际接受的参数不一致。未来如果接入其他 Agent 框架，也可以复用同一个 Registry。

## Q4：`CapabilityRegistry` 解决什么问题？

它是工具的统一边界，负责注册工具名称和 schema、校验模型参数、校验调用者 principal、执行只读能力，并校验工具返回值。

例如用户上传的数据不能由模型直接读取。模型只能提出 `describe_metadata`，Registry 和 runtime 会继续校验用户身份、dataset owner 以及文件 checksum。

## Q5：为什么当前工具执行不直接走 MCP？

MCP 解决工具发现和跨进程调用，OpenAI Tool Calling 解决模型如何提出工具调用，它们不是同一层。

当前工具都在同一个 Agent runtime 进程内，直接调用 Registry 延迟更低、链路更短。MCP 作为扩展边界保留：如果未来工具拆成独立服务、需要跨语言或供多个 Agent 共享，再把 Registry 暴露成 MCP Server。

## Q6：用户上传数据时，如何保证安全？

模型不接触原始文件，也不能把 `user_id` 当作可信参数传入工具。数据访问链路是：

```text
上传文件 -> 后端保存 owner/checksum -> runtime 校验 -> 工具返回有界 JSON -> 模型回答
```

工具执行前会重新校验用户归属、文件 checksum 和参数；工具结果限制字段、行数和字符数，避免把原始数据或其他用户数据泄露给模型。

## Q7：多步工具调用的消息怎么组织？

首轮发送稳定的 system 和 user context，后续追加标准消息：

```text
assistant: tool_calls
tool: tool_call_id + result
assistant: tool_calls
tool: tool_call_id + result
assistant: final answer
```

本轮 transcript 由业务图持有，assistant 消息原样保存，工具结果使用模型返回的真实 `tool_call_id`。这样模型能看到完整操作历史，vLLM 也可以复用稳定前缀。

## Q8：为什么使用 vLLM Prefix Caching？

多步工具循环中，system、工具定义和首轮 context 通常保持不变。Prefix Caching 可以复用这部分前缀的 KV 状态，降低重复 prefill 计算和端到端延迟。

它不是“只发送增量 HTTP 请求”，所以我通过 `prompt_tokens`、cache hit、prefill 延迟和总耗时共同评估收益，而不是只看 prompt token 数。

## Q9：为什么关闭 `structured_tool_response`？

当前生产优先保证原生工具调用稳定，因此请求使用：

```text
tools + tool_choice=auto
```

`structured_tool_response` 打开后会额外发送 `response_format=json_schema`。虽然 vLLM 接受这个组合，但在 `tool_choice=auto` 下模型有时会直接生成最终 JSON，跳过本应调用的数据工具。

因此当前选择是：工具调用走原生 `tool_calls`，最终回答走 assistant content，再由业务边界校验。若未来需要所有业务路由都强结构化，可以把 `run_analysis`、`query_result`、`ask_user` 等动作也建模为原生 function tools。

## Q10：如何验证这个设计有效？

我会做四类验证：

1. 协议测试：检查 `tool_calls`、真实 call id 和 assistant/tool 回放；
2. Schema 测试：用真实业务工具参数和 response model 验证；
3. E2E 测试：上传 metadata，调用 `describe_metadata` 或 `enumerate_contrasts`，检查权限、checksum 和最终回答；
4. 性能测试：观察 prefix cache queries/hits、模型延迟、工具延迟和重复调用拦截率。

## Q11：这个方案的主要工程取舍是什么？

优势是协议标准、工具边界清晰、数据安全责任在后端、便于监控和扩展；代价是需要维护 transcript、工具 schema 和模型兼容性测试。

对于一个处理用户上传科研数据的 Agent，我更看重可审计、可控权限和错误可恢复性，而不是让模型直接拥有执行能力。

## 一句话总结

“我让模型只负责提出标准化工具调用，让后端 Registry 负责 schema、权限、checksum 和执行，再用 LangGraph 管理业务状态，用 vLLM Prefix Caching 降低多轮工具循环的重复计算，从而在安全、可观测性和延迟之间取得平衡。”

## Q12：用户说“不要了/不做了”时，pending 是模型判断还是关键词判断？

当前不使用取消关键词改变 pending 状态。用户消息即使包含“不要”“不用”“取消”等词，也先交给模型根据上下文回答，避免“不要只看结果，继续分析”这类自然语言被误清空。普通澄清状态只保留 `active`、`consumed`、`superseded`、`expired` 四种生命周期状态。

后续如果需要支持显式取消，应让模型输出受限的结构化意图，例如 `resume_analysis`、`cancel_pending`、`new_intent`、`answer_unrelated`，再由后端状态机校验；不要恢复简单关键词删除逻辑。

## Q13：Agent runtime 里还有哪些关键词匹配？

当前关键词/字符串规则主要有四类：

1. `model.py` 的 `_is_explicit_jobs_listing()`、`_is_explicit_job_status_request()`、`_is_explicit_analysis_request()`：用于识别用户明确要求列出 Job、查询 Job 状态或发起分析，并修正模型输出的 action。这是模型输出纠偏，不是主业务意图的唯一来源。
2. `main.py` 的 `_should_retry_followup()`：识别“改短一点”“改正”“用通俗语言”等 follow-up 表达，决定是否给模型一次受限重试。
3. `param_resolver.py` 的 `_REFERENCE_LEVEL_MARKERS`：识别 `control/ctrl/wt/对照/未处理` 等常见 reference level。这是数据领域规则，不负责聊天路由。
4. 前端 `CopilotPage.tsx` 的 `guessField()`：根据文件名猜测上传角色，只是 UI 默认值，用户仍然可以手动修改。

此外大量 `lower()`/`casefold()` 只用于状态、checksum、文件扩展名和错误文本规范化，不属于意图判断。设计原则应是：模型负责开放式语义理解，规则负责安全边界、协议纠偏和可证明的数据语义。

## Q14：上传数据 -> 模型澄清 -> 用户回答 -> 路由错误是怎样发生的？

典型链路如下：

```text
上传 counts/metadata
  -> main_node 让模型调用 enumerate_contrasts
  -> 工具返回 control vs salt、salt vs control
  -> 普通聊天询问用户选择
  -> 用户回复“第二种”
  -> 新 turn 重新进入 main_node
```

旧实现只把澄清问题写进 `response_text`，没有持久化“正在等待哪个分析参数”。新 turn 虽然能看到 `recent_messages`，但同时还会继承 thread 的 `current_job`、`recent_jobs` 和 `focus.in_scope_job_ids`。当上下文中存在一个已取消 Job 时，8B 模型可能把“第二种”误判成已有结果查询，输出 `query_result`；图路由随后进入 `result_qa_node`，最后报 artifact 不存在。

这里不应把“存在 Job”当作“用户正在查询 Job”。短回答必须优先与 pending analysis 绑定，只有用户明确表达结果/状态意图时才进入结果路由。

## Q15：pending analysis 的生命周期应该怎样设计？

当前普通澄清建模为有版本和数据集关联的状态：

```text
缺参数产生       -> active
用户回答匹配     -> consumed，合并参数并回到 analysis
普通后续消息     -> pending 保持 active，由模型判断是否回答该澄清
重新上传数据     -> superseded，旧 pending 不再可恢复
数据集过期/变化  -> expired，pending 不再可恢复
```

`pending_analysis` 至少应记录分析类型、澄清问题、缺失字段、候选项、原始请求和 input bundle 版本。用户中途问“什么是 FDR”时可以保留 pending；用户稍后说“继续刚才分析”时，模型应读取该结构化状态，而不是只从历史文本猜测。重新上传相同或不同文件都应使旧 pending 失效，因为即使内容相同，也可能是用户明确开始了新的数据上下文。

## Q16：改进后的验证场景是什么？

至少要覆盖：

```text
上传数据 -> 模型询问 contrast -> 用户“第二种”
  -> 继续 analysis，不进入 result_qa，不读取旧 Job

上传数据 -> 用户提出包含“不要/不用”等词的其他问题
  -> 不做关键词清空，由模型根据上下文回答

模型询问 contrast -> 用户先问一个普通知识问题
  -> 回答知识问题，pending 保留
  -> 用户“继续刚才分析” -> 恢复分析上下文

模型询问 contrast -> 用户重新上传数据
  -> 旧 pending 失效，按新 bundle 重新推断
```

验收时应记录每个 turn 的 `decision.action`、`pending_analysis`、`dataset_profiles`、`current_job` 和 `recent_jobs`。重点断言：短回答不能因为旧 Job focus 被强制路由到 `query_result`；只有真正的分析参数齐全后，才进入 confirmation；只有真正的 Job/结果问题，才进入 `result_qa_node`。
