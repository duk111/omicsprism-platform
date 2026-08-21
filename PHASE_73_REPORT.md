# Phase 73 Report

## 目标与范围

S3 引入受控的 `system_facts` 叙述骨架，仅替换以下两条确定性提示路径：

- 待审批计划的通用提示。
- 输入预检失败提示。

模型可以根据服务端事实组织语言；schema、模型服务、超时、预算、事实或数字校验任一失败时，协调器返回原有提示文本并保持 turn 状态。其余 canned message、工具、profile、grounding 和 router 均未修改。

## 实现

- `ModelContext.system_facts` 只接受 `_ALLOWED_FACT_KEYS`，序列化上限 2 KB、嵌套深度上限 3，并拒绝 DSN、路径、原始 checksum、traceback 和多行原始内容。
- `AgentNarrationDecision` 固定 `action=answer`，副作用字段为空或 `false`，叙述上限 800 字符。
- narration 上下文清空工具、job、输入摘要、历史、证据和参数，只携带服务端 facts。
- narration 调用复用单 turn 模型预算；失败的模型尝试同样计数。
- narration 中出现的阿拉伯数字必须存在于 facts 的数值字段，否则回退。
- 待审批 facts 来自归属绑定的 plan/approval；读取失败直接回退，不生成未知事实。
- 预检错误最多传 3 条，每条最多 200 字符；角色列表有界。

## DoD 自查

- [x] `ModelContext.system_facts` 契约、白名单、2 KB、深度和敏感内容校验完成。
- [x] `AgentNarrationDecision` 无工具或写副作用能力。
- [x] 待审批通用提示接入 narration，审批、plan 和 job 状态保持不变。
- [x] 预检失败提示接入 narration，失败时仍不创建 plan/job。
- [x] narration 数字只能来自 facts 数值。
- [x] schema 错误、模型异常、预算耗尽和数字不匹配回退原文本。
- [x] 其余 canned message 和原 fallback 常量未删除或转换。
- [x] 后端、25-case eval、前端测试和前端构建通过。

## 红线自查

- R1：facts 经集中 schema 校验；narration context 不含工具句柄、原始文件、路径、凭据、完整日志或历史。
- R2：narration 的 `available_tools=[]`；未修改 profile 白名单、policy 或 registry。
- R3：plan 和 approval 均使用 `user_id` 归属绑定读取；未修改越权 404 行为。
- R4：narration 契约不能提交计划或改变审批；待审批测试确认 job 创建数为 0。
- R5：未修改结果 evidence/grounding；系统提示数字另由 facts 数字集合约束。
- R6：所有 narration 故障均回退现有文本，不让模型故障导致 turn 失败。

## 验证结果

后端全量回归：

```text
.venv/Scripts/python.exe -m pytest backend/tests -q --basetemp .pytest-tmp/s3-full-basetemp3
203 passed, 6 skipped in 4.75s
```

Unit eval：

```text
.venv/Scripts/python.exe -m scripts.run_agent_eval --assembly unit --output .pytest-tmp/phase73-unit.json
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
✓ built in 58.86s
```

## 已知缺口与后续切片依赖

1. 本 slice 只转换两条指定路径；其余六条确定性提示留给后续切片。
2. narration 的数字核查针对阿拉伯数字文本，不做自然语言数词换算；无法严格核查时应由调用方继续使用无数字表述或触发回退。
3. 前端构建保留既有 `InteractiveRouter` 大 chunk 警告，本 slice 未修改前端依赖或打包策略。
4. 本地测试使用 stub/fake 模型；真实 vLLM 可用性属于部署环境回归，不在本 slice 伪造验证。
