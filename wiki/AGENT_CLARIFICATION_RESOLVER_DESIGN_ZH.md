# 分析参数澄清解析子 Agent 设计草案

> 状态：设计记录，暂不实现。

## 背景

分析 Agent 在参数缺失时会向用户询问。用户可能用自然语言回答：

- “第二种”
- “就按后一个”
- “盐处理作为实验组”
- “继续前面的分析”

仅依赖主模型重新生成完整分析 proposal，容易受到多轮上下文、旧 Job 和表达歧义影响。

## 目标

增加一个专门的“澄清参数解析子 Agent”，只负责把用户回答映射到已有候选参数，不负责读取原始文件，也不负责提交 Job。

## 输入与输出

输入包括：

```json
{
  "user_reply": "那就用第二种方式",
  "pending_question": "请选择比较方向",
  "options": [
    {"id": "contrast-1", "label": "control vs salt"},
    {"id": "contrast-2", "label": "salt vs control"}
  ],
  "recent_messages": []
}
```

输出为受限结构化结果：

```json
{
  "matched_option_id": "contrast-2",
  "proposal": {
    "tested_level": "salt",
    "reference_level": "control"
  },
  "confidence": 0.96,
  "needs_clarification": false,
  "reason": "用户选择了候选列表第二项"
}
```

如果存在多个可能匹配、没有候选匹配或表达不明确，应返回 `needs_clarification=true`，不能自行猜测。

## 安全边界

子 Agent 只做语义映射，不能替代确定性校验。后续链路必须保持：

```text
用户回答
  -> 澄清解析子 Agent
  -> resolve_analysis_request
  -> validate_analysis_request
  -> confirmation
  -> 用户确认
  -> 提交 Job
```

确定性代码仍负责验证：

- 字段和 level 是否真实存在；
- 样本数和 replicate 是否满足要求；
- scope 是否可执行；
- 数据集归属、checksum 和输入版本是否有效。

即使子 Agent 返回错误参数，也必须在校验层被拦截，不能直接执行。

## 与多 Agent 架构的关系

该子 Agent 属于分析 Agent 的内部工具/子流程：

```text
分析 Agent
  ├─ 数据集事实提取
  ├─ 对比候选枚举
  ├─ 澄清参数解析子 Agent
  ├─ 确定性参数校验
  └─ confirmation
```

分析 Agent 可以看到 `pending_analysis` 和数据事实；结果解读 Agent 不需要加载这部分上下文，也不应调用该子 Agent。

## 后续实现要点

- 为每个候选生成稳定 `option_id`，避免依赖“第二种”对应的文本顺序变化；
- 将 `confidence`、匹配依据和候选冲突情况写入 trace；
- 低置信度、多候选匹配、历史方案冲突时继续询问用户；
- 子 Agent 不读取原始 CSV，只接收经过限制的结构化事实和候选；
- 增加“第二种”“前一个”“继续之前分析”“换实验组”等多轮回归用例。

