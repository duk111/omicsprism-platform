# Phase 74 Report

## 目标与范围

S4 将 S3 的 `system_facts` + `_narrate` 机制推广到其余六条 canned 文案路径，并为 preflight 失败补充有界样本名对齐诊断。原有常量全部保留为 fallback；`preflight.py` 校验逻辑、policy、工具权限和 grounding 未修改。

## 实现

- 待审批计划解释：传递分析类型、最多 5 条 contrasts 摘要、有效参数键值和审批剩余分钟数。
- 能力帮助：从 `AnalysisSpecRegistry` 生成 capability facts，并带当前输入角色。
- 输入回执：传递已有/缺失角色及每个角色的 row/column count。
- 计划作废：传递旧/新分析类型和变更参数键。
- 无任务状态：传递是否有输入和待审批计划。
- 任务失败：传递 job id、分析类型、`_sanitize_error_text` 脱敏文本和建议类别；不把 traceback、路径或 checksum 原文带入模型。
- `run_preflight` 结果增加有界 `alignment`：`matched`、两侧最多 10 个差集元素、每项最多 60 字符，以及代码判定的大小写/分隔符/统一前缀提示。
- 扩展统一 facts 白名单场景，不改变 `AgentNarrationDecision` 副作用约束。

## DoD 自查

- [x] 六条剩余 canned 文案逐一接入 `_narrate`，并保留原 fallback。
- [x] 模型不可用、schema 失败、预算耗尽或敏感校验失败时回退原文。
- [x] preflight alignment 差集和 `pattern_hint` 由服务端代码计算，不由模型推测。
- [x] alignment 差集有 10 项/60 字符边界，facts 不含原始文件内容。
- [x] 任务失败 facts 使用脱敏错误文本和建议类别。
- [x] 六类 facts 统一通过敏感串扫描。
- [x] 未修改 preflight 校验逻辑、policy、工具白名单、审批闸门和 grounding。
- [x] 后端、25-case eval、前端测试和构建通过。

## 红线自查

- R1：facts 继续经 `ModelContext` 白名单、长度、深度和敏感内容校验；任务错误先脱敏再进入 facts。
- R2：所有叙述上下文 `available_tools=[]`；未新增或放宽任何工具。
- R3：计划、审批和任务仍使用当前用户归属绑定；alignment 只读取当前 turn 输入。
- R4：叙述决策只允许 `AgentNarrationDecision`；计划作废和提交仍由原确定性状态机控制。
- R5：未修改结果 evidence、grounding、verifier；任务失败叙述不生成结果 claim。
- R6：模型不可用时六条路径返回原常量，不影响原分析、任务和结果流程。

## 验证结果

后端全量回归：

```text
.venv/Scripts/python.exe -m pytest backend/tests -q --basetemp .pytest-tmp/s4-full3
209 passed, 6 skipped in 5.13s
```

Unit eval：

```text
.venv/Scripts/python.exe -m scripts.run_agent_eval --assembly unit --output .pytest-tmp/phase74-unit.json
total=25 passed=25 failed=0 skipped=0 pass_rate=1.0
unapproved_job_creations=0.0 cross_user_access_successes=0.0
numeric_accuracy=1.0 citation_coverage=1.0
```

前端回归与构建：

```text
npm test -- --run
Test Files  2 passed (2)
Tests       10 passed (10)

npm run build
1849 modules transformed
✓ built in 59.62s
```

## 已知缺口与后续切片依赖

1. 本 slice 只改造 canned 文案叙述和 preflight 诊断透传，不改变固定控制流；模型主路由仍属于 S5。
2. 统一前缀提示仅对可解析的数字后缀样本名触发；无法确定时返回 `None`，避免猜测。
3. 前端构建仍有既有 `InteractiveRouter` 大 chunk 警告。
4. 本地模型不可用回退使用 stub/fake adapter；真实 vLLM 故障演示属于部署验收环境。
