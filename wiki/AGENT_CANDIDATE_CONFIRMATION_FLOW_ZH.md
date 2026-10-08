# OmicsPrism Agent 候选参数确认链路

本文定义分析请求从自然语言进入分析计划和 Job 提交前的目标交互链路。它是 `wiki/how_to_do.md` 的专项补充，重点解决以下问题：

- 模型把用户尚未确认的参数当成事实；
- 模型错误选择比较字段、比较组或样本分层方式；
- 用户只能看到笼统的 `ask_user`，不知道系统缺什么；
- 分析能力回答扩展出平台没有注册的能力；
- 示例数据中的字段名被误当成平台事实；
- 用户无法修改模型已经提出的候选参数。

本文只定义目标行为和实施路线，不代表当前代码已经全部实现。

## 1. 基本原则

### 1.1 用户字段名不是平台事实

用户上传的 metadata 字段由用户自行命名。平台不得把任何一个具体用户示例字段名写入路由、能力判断、参数解析器或 validator 作为事实依据。

例如，`treatment`、`timepoint`、`replicate`、`line` 等只能出现在：

- 测试 fixture；
- 生物信息学格式说明中的示例；
- 模型根据当前文件实际解析出的候选字段；

不能作为平台内置字段、默认比较字段或默认分层字段。

平台真正依赖的是：

```text
上传文件的规范角色
  -> 文件结构解析
  -> 当前文件实际存在的列名、水平、样本行和对齐关系
  -> 用户确认后的参数
```

规范角色是上传文件的业务角色，例如 `counts`、`metadata`、`metabolome`、`transcriptome`、`group`；它们不是 metadata 内部列名。

### 1.2 模型推断不是用户确认

模型可以提出候选参数，但候选参数必须显式标记为：

```text
candidate / inferred / awaiting_user_confirmation
```

在用户确认之前，系统不得说：

- “你要比较的是……”；
- “你的数据每组只有一个样本”；
- “该数据不能做差异分析”；
- “已经确定按某字段分层”。

正确表达应是：

```text
我根据当前文件和你的请求提出一个候选设置，请确认或修改。
```

### 1.3 用户可见回复不能暴露内部 action 名

`ask_user`、`tool_call`、`reroute` 等是内部协议 action，不是用户语言。

用户界面不得直接显示：

```text
ask_user
tool_call
reroute
```

内部 action 必须转换为以下用户可见结果之一：

```text
completed
needs_input
unsupported
unresolved
failed
```

普通业务澄清使用 `needs_input`，并由模型根据结构化事实组织当前语言的自然语言回复。只有模型、工具、网络、数据库或图执行失败时，才使用 `failed` 和稳定错误码。

## 2. 目标控制链路

分析类请求统一进入以下链路：

```text
用户分析请求
  -> 全局路由 target=analysis
  -> Analysis Agent 提出候选参数
  -> 确定性 resolver 校验候选字段/水平/分析类型
  -> 确定性 evaluator 计算真实样本计数和对齐事实
  -> 模型根据结构化事实生成候选确认回复
  -> 用户确认或修正
  -> 参数澄清 resolver 解析用户修正
  -> 再次执行 resolver + validator + 样本计数
  -> 生成分析计划
  -> 用户确认提交
  -> checksum/ownership/fingerprint/idempotency 校验
  -> 提交 Job
```

能力咨询不进入参数确认链路：

```text
用户能力问题
  -> analysis/capability_query
  -> AnalysisSpecRegistry 能力 evaluator
  -> 模型根据 CapabilityReport 组织回答
```

能力回答只能说明平台已注册分析和缺少的输入角色，不能根据 metadata 列名自行增加“时间点分析”“重复性分析”等未注册能力。

## 3. 候选参数协议

### 3.1 模型输出

Analysis Agent 对分析请求只能输出以下两类结果：

```text
candidate_plan
  或
needs_input
```

推荐的候选结构：

```json
{
  "action": "propose_plan",
  "analysis_type": "DEG",
  "proposal": {
    "compare_field": "<当前 metadata 中真实存在的列名>",
    "tested_level": "<当前列中真实存在的水平>",
    "reference_level": "<当前列中真实存在的水平>",
    "scope": {
      "mode": "all | stratified | fixed",
      "blocking_fields": [],
      "fixed_filters": {}
    },
    "min_replicates": 2
  }
}
```

字段名和水平只能来自当前 `GraphState` 的 bounded metadata facts 或工具 observation，不能来自模型记忆、平台默认值或其他用户会话。

### 3.2 候选参数的限制

模型不得：

- 直接提交 Job；
- 直接声称校验通过；
- 直接声称样本数不足；
- 自行增加新的分析类型；
- 把任意看起来像重复编号的字段强制解释为分层字段；
- 把任意看起来像时间的字段强制解释为时间序列分析；
- 把字段名相似当成字段语义已经确认。

对于生物信息学常见命名，提示词可以提供**语义参考**，但必须使用非事实性的措辞：

```text
在常见实验设计中，表示重复编号、批次、时间点、处理条件或品系的列可能具有不同统计语义；
不要仅根据列名决定比较因素或 blocking factor。请结合用户语言、文件结构和可验证的样本计数提出候选，并要求用户确认。
```

提示词可以展示抽象例子，例如“重复编号列”或“处理条件列”，但不得把某个示例列名注册为系统规则。

## 4. 确定性事实层

模型候选参数必须经过确定性事实层。事实层至少计算：

```text
analysis_type 是否已注册
compare_field 是否存在
tested/reference 是否存在且不同
scope 字段是否存在
scope 是否包含 compare_field
每个 scope strata 的 tested_count/reference_count
min_replicates 是否满足
counts 与 metadata 的样本对齐
dataset ownership
checksum
```

其中样本计数必须来自 metadata 的实际记录行，而不是模型估计或 profile 聚合猜测。

事实层输出示例：

```json
{
  "candidate": {
    "analysis_type": "DEG",
    "compare_field": "<observed field>",
    "tested_level": "<observed level>",
    "reference_level": "<observed level>",
    "scope": {
      "mode": "stratified",
      "blocking_fields": ["<observed blocking field>"]
    }
  },
  "validation": {
    "ok": true,
    "missing": [],
    "blocking": [],
    "warnings": []
  },
  "strata_counts": [
    {
      "stratum": {"<field>": "<value>"},
      "tested_count": 2,
      "reference_count": 2,
      "included": true,
      "exclusion_reason": null
    }
  ],
  "confirmation_required": true
}
```

### 4.1 事实层和模型的边界

事实层负责：

- 判断参数是否存在；
- 计算样本计数；
- 判断是否满足最低重复数；
- 判断是否可进入分析计划；
- 生成稳定的 blocking issue 和 failure code。

模型负责：

- 解释候选设置；
- 解释样本计数；
- 说明需要用户确认的原因；
- 使用用户当前语言组织回复。

模型不得修改事实层输出。如果模型回复与事实不一致，应丢弃模型回复并使用结构化事实的安全 fallback。

## 5. 用户可见交互

### 5.1 首次分析请求

用户说：

```text
帮我分析一下差异基因
```

系统不应直接执行，也不应只返回空泛的 `ask_user`。应该返回 `needs_input`，内容至少包括：

1. 分析类型候选；
2. 比较字段候选；
3. 测试组/参考组候选；
4. scope 候选；
5. 每个 strata 的真实样本数；
6. 需要用户确认或修改的字段。

自然语言由模型生成，事实来自确定性 evaluator。

目标语义示例：

```text
我根据当前 metadata 提出了一个候选 DEG 设置：

- 比较字段：<observed field>
- 测试组：<observed level>
- 参考组：<observed level>
- 样本范围：全部样本 / 按 <observed field> 分层
- 每个比较层的样本数：……

请确认这个设置，或告诉我需要修改比较字段、比较组或分层方式。
```

### 5.2 用户确认

用户可以使用：

```text
正确，继续
确认
就按这个做
```

确认后必须再次读取当前 pending candidate，并进入确定性计划生成，不得依赖模型重新猜一遍。

### 5.3 用户修正

用户可以说：

```text
换一个比较组
按另一个字段分层
不要分层
这个字段不是处理条件
把测试组和参考组交换
降低最少重复数
```

参数澄清子 Agent 将其解析为受限 patch：

```json
{
  "intent": "edit_params",
  "proposal_patch": {
    "scope_mode": "stratified",
    "blocking_fields": ["<observed field>"]
  }
}
```

patch 应用后必须重新执行完整事实层校验。澄清子 Agent 不负责统计、验证或提交。

### 5.4 无关问题

如果用户在 pending candidate 期间问知识问题，例如解释 FDR：

- 当前问题可以进入 QA；
- pending candidate 保留；
- QA 回复不得覆盖候选参数；
- 用户说“继续刚才分析”时恢复候选确认。

## 6. `ask_user` 的处理原则

内部可以保留一个 action 名称表示“需要用户输入”，但它不能直接出现在用户消息中。

内部 action 到公开结果的映射必须是：

```text
需要候选参数确认/修改 -> outcome=needs_input, failure_code=missing_analysis_parameter
无法识别请求         -> outcome=unresolved, failure_code=route_ambiguous
能力明确不支持         -> outcome=unsupported, failure_code=unsupported_request
模型/工具/网络失败      -> outcome=failed, 使用对应稳定 failure_code
```

当模型正常运行但返回 `ask_user` 时，系统必须把它转换成自然语言的 `needs_input` 回复；不能把 action 名、Python 枚举名或内部调试字符串直接放进 response text。

如果模型正常返回但没有提供足够参数：

1. 优先使用确定性 resolver 根据事实生成缺失项；
2. 模型只负责把缺失项组织成自然语言；
3. 只有无法从事实层生成安全问题时，才使用通用的 `needs_input` 文案。

## 7. 模型提示词改造方向

Analysis prompt 必须明确：

```text
你提出的是候选参数，不是用户已经确认的实验设计。
列名只代表当前文件中的观测名称，不代表平台内置语义。
不要因为存在某个列名就扩展平台能力。
不要根据列名单独决定比较字段、参考组或分层字段。
不要把重复编号字段自动当成 blocking field。
必须优先使用提供的确定性 strata_counts 和 validation facts。
如果候选参数未被用户确认，必须请求确认，不得声称分析已经可运行或已经执行。
使用用户最近一条消息的语言回复。
```

Few-shot 应覆盖：

1. 只有 `counts + metadata` 时只报告已注册的 DEG 输入能力；
2. metadata 中出现多个实验因素时不自动选择唯一比较因素；
3. 重复编号字段不得自动成为分层字段；
4. 用户修正 scope 后重新确认样本计数；
5. 模型候选与事实层结果冲突时，以事实层为准；
6. 英文用户得到英文自然语言回复，中文用户得到中文回复。

## 8. 状态模型

建议在 `GraphState` 中增加或明确以下当前 turn 字段：

```python
candidate_plan: PendingPlan | None
candidate_validation: ValidationReport | None
candidate_strata_counts: list[StratumSummary]
candidate_confirmation_status: Literal[
    "none", "awaiting_user", "confirmed", "rejected", "superseded"
]
```

状态转换：

```text
none
  -> awaiting_user       模型提出候选且事实层验证完成
  -> confirmed           用户确认候选
  -> rejected            用户拒绝候选
  -> superseded          用户上传新数据或提出新分析
```

只有 `confirmed` 的候选才能生成待提交的 `PendingPlan`。只有用户对 `PendingPlan` 的最终提交确认，才能执行 Job submitter。

## 9. 错误和可观测性要求

在 trace 完整建设之前，至少要把以下信息写入当前 turn 的 bounded trace 或 tool observation：

```text
route target
entered role
model action
analysis_type
candidate compare_field/tested/reference
candidate scope
validator result
strata counts
tool name
tool arguments hash
tool failure code
user confirmation or patch
```

不得记录原始 CSV 行、密钥或完整 prompt。工具失败的用户回复应由模型基于安全错误事实生成，但内部必须能回答：

```text
哪个工具失败？
使用了什么参数？
哪个校验失败？
是否重试？
失败前是否已有候选参数？
```

## 10. 实施顺序

1. 移除所有对用户可见的 `ask_user`、`tool_call`、`reroute` 原始字符串。
2. 将普通 `ask_user` 收敛为公开 `needs_input`，统一由模型根据结构化 facts 组织语言。
3. 增加候选参数确认状态和 `candidate_strata_counts`。
4. 确保 metadata 行级样本计数由确定性代码计算，不由模型估计。
5. 修改 Analysis Agent schema：分析请求优先返回候选参数，不允许无事实依据直接输出最终分析结论。
6. 将用户确认和用户修正连接到 clarification resolver；修正后强制重新验证。
7. 在 prompt 中加入通用生物信息学文件格式和实验设计语义说明，但不写入任何具体用户字段名规则。
8. 增加中英文回归测试：
   - 只有 counts + metadata；
   - metadata 有多个实验因素；
   - 重复编号字段；
   - 用户确认候选；
   - 用户纠正 scope；
   - 用户问无关知识后继续分析；
   - 模型失败、工具失败和网络超时。

## 11. 验收标准

### 场景 A：能力咨询

输入只有 `counts + metadata` 时：

- 回复只基于已注册 AnalysisSpec；
- 不声称 DEM/GMA 已可运行；
- 不把 metadata 列名扩展成新的分析类型；
- 使用用户语言回复。

### 场景 B：差异基因请求

- 不直接执行 Job；
- 不直接声称用户意图或数据结论；
- 展示候选比较设置；
- 展示确定性样本计数；
- 请求用户确认或修正；
- 页面不出现 `ask_user`。

### 场景 C：用户修正

- 自由语言修正被转换成受限 patch；
- patch 只允许修改当前 metadata 中存在的字段/水平；
- 修改后重新计算 strata counts；
- 未确认前不生成 Job。

### 场景 D：失败

- 模型、工具、网络失败使用稳定 failure code；
- 不把失败伪装成数据能力判断；
- 不把内部异常名直接显示给用户；
- 用户看到当前语言的自然语言错误说明或可恢复操作。

