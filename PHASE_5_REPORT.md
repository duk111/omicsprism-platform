# Phase 5 Report

## 当前状态

Phase 5 实现已完成本地验证；live vLLM、真实 PostgreSQL production replay 和三项人工演示待服务器执行。未配置 live endpoint 时只生成显式 skip，不记录伪造的 live model 成绩。

## 已实现

- 建立 25 个 schema 校验的 golden case：router 5、recommendation 4、contrast/approval 4、failure 3、grounding 9。
- 覆盖全部必带对抗案例：union 方向、EdgeWeight/相关系数、OPLS-DA score/VIP、空 padj、跨用户 404、因果越证据和单元格注入。
- 同一 `EvalRunner` 支持 `unit`、`offline`、`production` 三装配；live 配置缺失时整套显式 skip。
- `VllmModelAdapter` 通过 OpenAI-compatible `/v1/chat/completions` 调用，只发送 `ModelContext`，使用严格 JSON Schema 并关闭 Qwen3 thinking，返回值再次强制校验为 `AgentDecision`。
- 推荐上下文包含结构化 `available_input_roles` 与由唯一 `AnalysisSpecRegistry` 生成的 required-input capability；模型只可推荐完整满足输入规则的分析，不从自然语言补猜缺失角色。
- production 跨用户 case 使用真实 `PostgresJobRepository` 和普通 runtime DSN；未通过 ownership 校验前不读取 artifact。
- 实现 JSON eval report 与 replay diff，包含通过率、模型调用数、P95 延迟、指标差异、新失败、恢复和新 skip。
- README 补充架构图、三装配、服务器命令；中文演示文档给出 8–10 分钟脚本。

## DoD 自查

- [x] golden case 数量与分布达到 spec 第 11 节要求，全部必带对抗案例可确定性 replay。
- [x] unit stub/fixture 快速集可在普通 CI 运行；live 装配缺 endpoint 时明确 skip。
- [x] 本地 unit 指标达到门槛：route/recommendation/contrast/numeric/citation 均为 1.0，未审批创建 job 与跨用户访问成功数均为 0。
- [x] 同一 case 集可在两个报告间生成机器可读 diff，CLI 路径有自动化测试。
- [x] README、Mermaid 架构图、服务器命令和 8–10 分钟演示脚本与当前实现一致。
- [ ] 服务器完成跨用户 404、关闭 vLLM 后手工分析可用、换模型 replay 三项人工演示。

## 本地验证

```text
Phase 5 model/eval directed: 20 passed
unit eval: 25 passed, 0 failed, 0 skipped
full suite: 83 passed, 2 skipped in 3.32s
compileall: passed
git diff --check: passed
```

unit eval 指标：

```text
pass_rate: 1.0
model_calls: 4
route_accuracy: 1.0
recommendation_accuracy: 1.0
contrast_block_rate: 1.0
unapproved_job_creations: 0.0
cross_user_access_successes: 0.0
numeric_accuracy: 1.0
citation_coverage: 1.0
```

两个 skip 是未配置专用 PostgreSQL 测试库环境变量时的既有权限测试；真实数据库权限已经在前序 Phase 的服务器验收中通过。当前本地工作区还保留用户原有的 4 个 Markdown 删除、`backend/tests/test_phase2_control_plane.py` 删除和 2 张未跟踪 PNG，均不属于 Phase 5，也不会纳入提交。

## 首次 live model baseline

服务器使用 vLLM 0.8.5、Qwen3-14B-AWQ、单张 RTX 3090 完成首次 offline replay：

```text
22 passed, 3 failed, 0 skipped
schema_validity: 1.0
recommendation_accuracy: 0.25
route/contrast/numeric/citation: 1.0
unapproved_job_creations: 0.0
cross_user_access_successes: 0.0
```

三个失败均为模型在 DEM、GMA、DEG+DEM case 中额外推荐了输入条件不满足的分析。该真实失败没有标记为通过；它触发了 typed capability context 修复。修复后的 live replay 与 baseline diff 待服务器执行。

## 服务器验证待办

1. 运行 unit eval，确认无外部服务时 25 case 全部执行。
2. 拉取 typed capability context 修复，使用首次 live 报告作为 baseline 重跑 offline replay 并生成 diff。
3. 用 `omics_app` DSN、他人 job id 和请求用户 id 跑 production，确认跨用户 case 为 404。
4. 关闭 vLLM，通过原手工表单完成一次提交/查询，确认 R6。
5. 更换模型或 prompt 后 replay，并人工审阅 diff。

## 已知缺口

- 当前没有可用的本地 vLLM endpoint，因此本报告不包含 live model 成绩。
- production replay 只应在受控服务器使用准备好的跨用户 fixture job；不能对真实用户数据做开放式探索。
- 管理 UI、通用 RAG/SQL/shell、新业务工具和独立多-agent 服务均不在 Phase 5 范围内。
