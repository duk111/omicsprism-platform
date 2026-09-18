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
