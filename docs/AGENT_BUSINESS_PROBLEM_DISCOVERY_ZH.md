# OmicsPrism Agent 业务问题发现

> 创建日期：2026-08-26  
> 状态：问题发现与目标契约，非实现方案  
> 范围：v3 Agent 的分析请求理解、参数解析、metadata 感知、歧义处理与确认交互

## 1. 文档目的

本文档记录当前 Agent 在真实分析场景中暴露出的业务问题，并从 Agent 落地所需的能力边界出发总结根因。

## 2. 核心结论

当前系统已经具备 typed proposal、deterministic resolver、validation、confirmation 和 HITL interrupt 的技术骨架，但这些部件之间还没有形成完整的业务闭环：

```text
用户科学意图
  -> Agent 识别请求类型和参数角色
  -> 读取受限且足够的 DatasetProfile
  -> 生成候选分析请求
  -> 根据真实 metadata 验证字段、值、分组和 replicate
  -> 判断是否唯一
  -> 唯一且用户意图明确：confirmation
  -> 多个合理解释或意图不足：clarification HITL
```

目前容易出现的实际路径是：

```text
用户消息
  -> 模型只看到有限上下文
  -> 模型路由或 proposal 不完整
  -> resolver 用值出现规则补齐部分参数
  -> 空的可选字段被当作无约束
  -> 生成一个看似合法但范围可能错误的 plan
```

因此当前最重要的问题不是“模型是否输出了合法 JSON”，而是：**合法 JSON 中的参数是否表达了用户想做的科学比较。**

## 3. 业务请求的真实组成

一个 DEG/DEM 请求至少包含以下不同语义：

| 语义 | 示例 | 作用 |
| --- | --- | --- |
| 分析类型 | DEG | 选择分析能力 |
| 比较字段 | `treatment`、`arm`、`condition` | 决定在哪个 metadata 字段上比较 |
| 实验组 | `salt`、`drug`、`B` | 被测试的水平 |
| 参考组 | `control`、`vehicle`、`A` | 比较基准 |
| 固定条件 | `line=I1853`、`timepoint=24h` | 只在指定子集内比较 |
| 分层字段 | `line`、`timepoint` | 在每个条件组合内分别比较 |
| 范围声明 | 全部样本、某个子集、每个 strata | 决定分析覆盖范围 |

固定条件和分层字段不是同一件事：

```text
在 I1853、24h 下比较 salt 和 control
  -> 固定条件：line=I1853, timepoint=24h

在每个 line 和 timepoint 内分别比较 salt 和 control
  -> 分层字段：line, timepoint
```

如果系统没有区分这些语义，即使生成的 `compare_field`、`tested_level` 和 `reference_level` 都正确，分析仍可能使用错误的样本范围。

## 4. 问题一：请求类型路由与分析意图不稳定

### 4.1 现状

主模型首先在 `answer`、`inspect_dataset`、`run_analysis`、`query_result`、`get_job` 和 `ask_user` 之间选择一个 action。只有进入 `run_analysis` 或 `inspect_dataset`，后端 resolver 才会处理分析参数。

因此，类似“帮我对比同一 line 和 timepoint 下的 salt 和 control”的消息，如果被路由成一般知识回答，后续确定性解析根本不会执行。用户看到的是知识回答，而不是分析计划或澄清问题。

### 4.2 本质问题

当前路由把“用户想讨论一个分析问题”和“用户授权生成可执行分析请求”之间的边界交给一次模型分类，但没有提供足够的数据语境和业务判定规则。

这不是单纯的模型准确率问题，而是 capability contract 不完整：

- 何时应该进入分析路径；
- 何时只是解释分析概念；
- 何时用户已经给出了执行意图但参数不完整；
- 何时应该先 inspect dataset 再询问；

这些情况必须有稳定的产品行为。

### 4.3 业务风险

- 用户明确要求分析却得到泛化回答；
- 同一用户消息因模型一次分类不同而走不同业务路径；
- 分析参数问题被掩盖在“生物学知识”回复中；
- 评测只看 JSON 合法性时无法发现路由错误。

## 5. 问题二：模型缺少足够的 metadata 语境

### 5.1 现状

`MetadataProfile` 已经在 graph state 中保存了 metadata 的字段、水平、计数、样本 ID、行数据和 alignment 信息。但当前主模型上下文主要传递的是：

```text
user_message
conversation_summary
dataset_roles
current_job_id
recent_job_ids
```

其中 `dataset_roles` 只说明存在 `metadata`、`counts` 等角色，不等于模型看到了：

```text
columns
levels
每个水平的计数
可能的比较字段
可能的分层字段
```

### 5.2 本质问题

系统要求模型提出数据相关的候选参数，却没有把执行该语义任务所需的最小事实上下文交给模型。

模型无法可靠知道：

- `line` 是否真实存在；
- `I1853` 是否是 `line` 的真实水平；
- `treatment` 是否只有 `salt/control` 两个水平；
- 某个固定条件组合是否有足够 replicate；
- 是否存在多个同样合法的 contrast。

这导致模型只能依赖用户文本中的词面或通用知识，无法完成真正的数据感知意图理解。

### 5.3 边界要求

不能把完整 CSV、原始矩阵或无限 metadata 直接塞进 prompt。需要提供受限、类型化、足够完成语义判断的画像，例如字段、水平、计数、对齐状态和由 Python 预计算的候选 contrast。原始事实的最终裁判仍必须是 Python。

## 6. 问题三：词面匹配被误当成意图理解

### 6.1 当前逻辑

resolver 的 `_apply_message_levels()` 会遍历 metadata 中真实存在的值，检查这些值是否出现在用户原文中。它可以发现：

```text
salt 和 control 出现在 treatment 列
WT 出现在 line 列
24h 出现在 timepoint 列
```

然后根据“某字段出现两个值”或“某字段出现一个值”的规则形成候选参数。

### 6.2 本质问题

出现过一个词，不等于用户把它赋予了某个分析角色。

例如：

```text
我的研究对象是 WT 品种，在 24h 观察 drug 处理
```

这里的 `WT`、`24h` 和 `drug` 可能只是实验背景，不一定表示：

```text
compare_field = treatment
tested_level = drug
same_fields = {variety: WT, timepoint: 24h}
```

代码无法仅凭词面稳定区分以下语言角色：

- 要比较的两个水平；
- 作为筛选条件的值；
- 用户描述的实验背景；
- 用户举例或引用历史结果；
- 用户要求“每个条件内分别比较”的分层字段。

### 6.3 业务风险

- 背景信息被误认为比较参数；
- 真正的固定条件没有被提取；
- 同一个值在不同语境下产生不同结果；
- 依赖英文关键词或精确 level 文本，无法覆盖真实用户表达。

词面匹配可以作为候选提取的辅助信号，但不能成为用户意图的最终裁判。

## 7. 问题四：可选字段、默认值和“未决定”被混为一谈

### 7.1 `same_fields` 的当前语义

`AnalysisProposal.same_fields` 当前是可选对象，默认值为空字典：

```json
"same_fields": {
  "additionalProperties": {
    "type": "string"
  },
  "title": "Same Fields",
  "type": "object"
}
```

模型省略它后，应用侧会得到 `{}`。

### 7.2 业务歧义

当前系统无法区分：

```text
模型没有判断样本范围
用户明确要求不限制其它字段
用户要求所有样本合并比较
用户要求按其它字段分层但未选择具体值
```

这些状态在业务上完全不同，但都可能落到空对象或空字符串。

### 7.3 通用问题

这不是 `same_fields` 独有的问题。任何具有默认值或允许为空的分析参数都需要区分：

```text
未指定（unknown）
明确无约束（explicit none）
系统默认（defaulted by policy）
从数据唯一推断（uniquely inferred）
需要用户选择（requires HITL）
```

如果不区分这些状态，系统会把“模型没填”静默解释成“用户同意默认行为”。

## 8. 问题五：固定条件与分层比较的契约混淆

### 8.1 两种合法请求

```text
在 line=I1853、timepoint=24h 下比较 salt 和 control
```

这是一个固定条件 contrast：

```json
{
  "line": "I1853",
  "timepoint": "24h"
}
```

```text
在每个 line 和 timepoint 内分别比较 salt 和 control
```

这是一个分层比较：

```json
{
  "blocking_fields": ["line", "timepoint"]
}
```

### 8.2 当前冲突

网页手工表单的 “Same fields (comma-separated)” 更接近字段名列表：

```text
line,timepoint
```

而 Agent 的 `AnalysisProposal.same_fields` 更接近字段到固定值的映射：

```json
{"line":"I1853","timepoint":"24h"}
```

同名字段承载了不同业务含义，导致模型、resolver、确认页面和执行层对同一参数的理解可能不同。

### 8.3 业务风险

- 用户要求分层比较，却只得到一个全样本 contrast；
- 用户要求一个固定子集，却被扩展到多个 strata；
- plan 无法说明到底使用了哪些样本；
- CLI、网页和 Agent 之间无法共享同一参数契约。

## 9. 问题六：候选枚举与唯一性判断的边界不清

### 9.1 枚举的正确职责

resolver 枚举所有候选 contrast，是为了回答：

```text
在真实 metadata 和 replicate 约束下，哪些比较方案可以执行？
```

它应检查：

- 字段真实存在；
- 水平真实存在；
- tested/reference 不相同；
- 每组 replicate 达到最低要求；
- 固定条件或 strata 组合真实存在；
- 分层后两组仍然可比较。

### 9.2 不能把候选数量当作意图证明

候选数量只能说明执行层面的选择数量，不能说明用户想要哪一个科学问题。

```text
唯一候选
```

不一定等于“用户明确授权了这个分析”。它可能只是当前数据恰好只有一个可执行方案。

```text
多个候选
```

也不一定只需要让用户从技术选项中点一个；系统应使用自然语言解释这些选项对应的科学范围。

### 9.3 三种结果的业务含义

| 内部结果 | 业务含义 | 正确产品行为 |
| --- | --- | --- |
| `resolved` | 参数完整、事实合法、且当前语义足够明确 | 生成 confirmation，不直接执行 |
| `clarification` | 有多个合理解释，或用户明确要求但范围未定 | HITL 询问并列出可理解的选择 |
| `missing/invalid` | 缺少必要信息或参数不符合真实数据 | HITL 说明缺什么、可选什么、为什么不能猜 |

`resolved` 不应被理解成“模型猜对了”，而应被理解成“系统已证明当前请求可以唯一落地”。

## 10. 问题七：非标准字段和值的支持不完整

### 10.1 事实层面可以支持任意标签

真实项目中的 metadata 可能使用：

```text
字段：arm、condition、group_id、regimen、strain
值：A/B、vehicle/drug、baseline/treated、WT/KO
```

只要字段和值真实存在，并且用户明确说明比较方向，resolver 可以验证并执行，不应要求必须叫 `treatment`、`salt` 或 `control`。

### 10.2 当前的启发式局限

系统对 `control`、`ctrl`、`ck`、`wt`、`mock`、`untreated` 等名称有参考组启发式。但 `vehicle`、`baseline`、`sham`、`placebo` 等业务上常见的标签不一定自动识别。

这类启发式只能用于提出候选，不能作为事实或实验语义的最终判断。

### 10.3 真实上线要求

- 字段和值的真实性必须由 metadata 验证；
- 非标准标签不能被强制映射成 control/treated；
- 用户未指定比较方向时必须询问；
- 自然语言同义词到真实字段的映射需要模型语义能力和候选事实支持，不能靠字符串相似度静默纠正。

## 11. 问题八：大 metadata 与上下文预算的矛盾

为了控制 prompt 和内存大小，当前画像只在 metadata 较小时保留原始 rows。大 metadata 可能只有字段和值计数，没有完整行级数据。

这带来一个真实的业务矛盾：

```text
必须限制上下文，不能发送完整数据
但多因素 replicate 合法性需要按 strata 查看行级组合
```

仅凭各列的边际计数无法证明：

```text
line=I1853 AND timepoint=24h 下确实同时存在 salt 和 control，且每组 replicate 足够
```

因此系统需要在“上下文画像”和“确定性计算服务”之间划清边界：大数据的分层候选应由 Python 计算出摘要或候选列表，模型只消费受限结果，不能通过放宽 prompt 预算解决。

## 12. 问题九：确认计划的可审计性不足

当前 plan 主要展示：

```text
comparison field
experimental group
reference group
样本数
```

但没有稳定展示：

- 固定条件；
- 分层字段；
- 实际 strata 数量；
- 每个 strata 的样本数；
- 参数是用户明确提供、模型提出还是系统唯一推断；
- 空约束是用户选择还是未决定。

这会使用户无法在执行前发现“比较字段正确，但样本范围错误”。

确认不是形式上的最后一步，而是用户对一个具体数据范围和统计比较的授权。因此 plan 必须能让用户审计：

```text
比较什么
在哪些样本中比较
排除了什么
是否按条件分层
每组有多少 replicate
```

## 13. 问题十：确认后的修改不是完整的语义回合

确认阶段用户可能输入：

```text
我希望可以设置 same_fields 参数
```

但这句话没有说明：

- 字段名是什么；
- 是否固定具体值；
- 是否要求每个 strata 分别比较；
- 是否要改变 compare field 或比较方向。

当前修改路径主要把这段文本交回 resolver，并不一定重新经过一次完整的模型语义提取。因此用户以为自己“修改了参数”，系统可能只是保留原 plan。

业务上需要把 confirmation modification 视为新的参数澄清回合：重新识别意图、重新验证 metadata、重新生成 plan，而不是把任意自由文本当作已结构化的参数更新。

## 14. JSON Schema 能保证什么，不能保证什么

当前系统使用 vLLM structured output 和 Pydantic 双重校验：

```text
vLLM response_format.json_schema
  -> MainModelOutput.model_validate_json()
  -> graph 中再次 MainModelOutput.model_validate()
```

这可以保证：

- JSON 可解析；
- action 属于允许的枚举；
- `same_fields` 是 object；
- object 的 value 是 string；
- 禁止未声明的顶层或嵌套字段；
- action 与 proposal/result query/answer 的结构关系满足模型校验器。

例如下面是结构合法的：

```json
{"same_fields":{"invented_column":"invented_value"}}
```

但它不一定是业务合法的，因为 JSON Schema 不知道 `invented_column` 是否存在于真实 metadata。

同样，下面也可能结构合法：

```json
{"same_fields":{}}
```

但它不代表用户明确选择了“所有样本”。

因此必须明确三层校验：

```text
Schema 校验：形状是否合法
resolver 校验：字段和值是否真实、候选是否可执行
业务语义校验：这个请求是否表达了用户想做的科学比较
```

模型调用或结构化校验失败时，当前主节点会重试一次；仍失败则 ask_user，不静默猜测。这解决的是模型故障，不解决结构合法但语义错误的问题。

## 15. 对 Agent 业务落地的目标契约

后续实现必须满足以下行为，而不是只追求某些 fixture 变绿。

### 15.1 模型上下文

模型应看到足以完成语义判断的受限 DatasetProfile 摘要，包括：

- 真实 metadata 字段；
- 每个字段的真实水平和计数；
- 字段角色或候选用途；
- Python 计算的可行 contrast/strata 摘要；
- 必要的 alignment 和 replicate 信息。

原始矩阵和完整 CSV 仍不得直接进入 prompt。

### 15.2 proposal 语义

proposal 必须能表达并区分：

```text
固定字段和值
按字段分层
明确不限制其它字段
尚未决定范围
```

模型只能提出候选，不能裁判字段真实性、replicate 或执行合法性。

### 15.3 resolver 行为

resolver 必须：

- 验证字段和值来自真实 metadata；
- 不对未知字段和值做模糊纠正；
- 不把任意出现的值直接当作比较参数；
- 能处理非标准字段名和水平；
- 能区分固定条件与分层比较；
- 在多个科学解释都成立时返回 clarification；
- 在用户没有授权默认范围时不静默使用全部样本。

### 15.4 HITL 行为

clarification 必须说明用户要做的选择，而不是只暴露内部字段名。例如：

```text
检测到 treatment 有 salt/control，且 line 与 timepoint 会形成多个合法样本层。
请选择：
1. 所有样本合并比较
2. 指定 line/timepoint 后比较
3. 在每个 line × timepoint 层内分别比较
```

### 15.5 confirmation 行为

confirmation plan 必须展示：

- analysis type；
- compare field；
- tested/reference；
- fixed conditions 或 blocking fields；
- 实际样本范围和每组 replicate；
- 系统推断的部分；
- 用户仍可修改的部分。

## 16. 验收标准

后续评测至少覆盖以下类别，而不是只覆盖标准 `control/salt` 示例：

| 类别 | 需要证明的行为 |
| --- | --- |
| 明确固定条件 | 用户给出 `line=I1853、timepoint=24h`，只在该子集比较 |
| 明确分层 | 用户要求每个 line/timepoint 内分别比较，生成多个 strata contrast |
| 明确全样本 | 用户明确说所有样本合并比较，允许空约束 |
| 范围未说明 | 多因素 metadata 下必须 HITL，不能默认全样本 |
| 非标准标签 | `vehicle/drug`、`A/B` 等不依赖 `control` 词也能在用户明确时运行 |
| 背景提及 | 只描述品种或处理背景时，不得自动把背景值当比较参数 |
| 两个比较字段 | `line` 与 `treatment` 都可比较时必须澄清 |
| 低 replicate | 分层后任一组不足时拒绝该候选并解释原因 |
| 大 metadata | 不发送完整原始数据，仍能基于 Python 摘要做正确候选判断 |
| 模型失败 | 结构化输出失败时重试一次，仍失败则询问，不猜参数 |
| 修改回合 | confirmation 修改后重新解析和验证，不能静默保留旧 plan |

每个用例应同时检查：

```text
action 是否正确
proposal 是否表达正确的参数角色
resolved 参数是否事实合法
same_fields/blocking_fields 是否正确
是否错误自动运行
plan 是否展示了完整范围
```

## 17. 结论

当前遇到的现象可以统一归纳为一个 Agent 落地问题：

> 系统还不能稳定地把“用户对实验设计的自然语言描述”转换成“经过真实 metadata 事实约束、范围明确、用户可确认的分析请求”。

解决这个问题需要同时处理：

```text
数据感知上下文
+ 意图路由
+ 参数角色识别
+ optional/default/unknown 状态区分
+ 固定条件与分层语义拆分
+ deterministic candidate validation
+ 多解 HITL
+ confirmation 可审计展示
+ 覆盖真实标签和真实实验设计的评测
```

只给 `same_fields` 增加字段说明，或只修改一条 prompt，不能单独完成这项业务闭环。字段说明是必要条件，但不是充分条件；最终可靠性来自模型语义候选、Python 事实校验和用户在执行前的明确确认共同作用。
