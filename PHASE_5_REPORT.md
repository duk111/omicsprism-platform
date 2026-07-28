# Phase 5 Report

## 当前状态

Phase 5 已完成。实现、本地验证、Qwen3-14B-AWQ offline/production replay、跨用户 404、关闭 vLLM 后的手工分析演示，以及 Qwen3-14B-AWQ 到 Qwen3-8B-AWQ 的真实换模型 replay/diff 均已完成。未配置 live endpoint 时只生成显式 skip，不记录伪造的 live model 成绩。

## 已实现

- 建立 25 个 schema 校验的 golden case：router 5、recommendation 4、contrast/approval 4、failure 3、grounding 9。
- 覆盖全部必带对抗案例：union 方向、EdgeWeight/相关系数、OPLS-DA score/VIP、空 padj、跨用户 404、因果越证据和单元格注入。
- 同一 `EvalRunner` 支持 `unit`、`offline`、`production` 三装配；live 配置缺失时整套显式 skip。
- `VllmModelAdapter` 通过 OpenAI-compatible `/v1/chat/completions` 调用，只发送 `ModelContext`，使用严格且有界的 JSON Schema、关闭 Qwen3 thinking 并限制最多生成 512 tokens，返回值再次强制校验为 `AgentDecision`。
- 推荐上下文包含结构化 `available_input_roles` 与由唯一 `AnalysisSpecRegistry` 生成的 required-input capability；模型只可推荐完整满足输入规则的分析，不从自然语言补猜缺失角色。
- production 跨用户 case 使用真实 `PostgresJobRepository` 和普通 runtime DSN；未通过 ownership 校验前不读取 artifact。
- 实现 JSON eval report 与 replay diff，包含通过率、模型调用数、P95 延迟、指标差异、新失败、恢复和新 skip。
- README 补充架构图、三装配、服务器命令；中文演示文档给出 8–10 分钟脚本。

## DoD 自查

- [x] golden case 数量与分布达到 spec 第 11 节要求，全部必带对抗案例可确定性 replay。
- [x] unit stub/fixture 快速集可在普通 CI 运行；live 装配缺 endpoint 时明确 skip。
- [x] unit 与 Qwen3-14B-AWQ offline live 指标达到门槛：schema/route/recommendation/contrast/numeric/citation 均为 1.0，未审批创建 job 与跨用户访问成功数均为 0。
- [x] 同一 case 集可在两个报告间生成机器可读 diff，CLI 路径有自动化测试。
- [x] README、Mermaid 架构图、服务器命令和 8–10 分钟演示脚本与当前实现一致。
- [x] 服务器已完成 production 跨用户 404、关闭 vLLM 后手工分析可用、同模型 prompt/schema replay 和 Qwen3-14B-AWQ 到 Qwen3-8B-AWQ 的真实换模型 replay。

## 本地验证

```text
Phase 5 schema/model/eval directed: 43 passed
unit eval: 25 passed, 0 failed, 0 skipped
full suite: 88 passed, 2 skipped in 3.41s
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

typed capability context 的首次 replay 为 23 passed、2 failed；`recommend_deg_dem_001` 恢复，且无新增失败。两个失败均为 DEM/GMA 请求在客户端固定 60 秒处发生 `ReadTimeout`，不是 schema 或推荐断言错误；P95 因此为 48.45 秒。该结果触发了 512-token 输出硬上限，限长后的 live replay 待服务器执行。

加入 512-token 上限后的 replay 仍为 23 passed、2 failed，但 P95 从 48.45 秒降至 5.80 秒。vLLM 日志证明四次请求均为 HTTP 200，GPU generation throughput 约 55–70 tokens/s；DEM/GMA 生成满 512 tokens 后返回截断 JSON，使 schema validity 降至 0.5。根因是原 `AgentDecision` schema 的字符串、数组和任意递归 `requested_params` 没有边界，而不是 GPU 或 scheduler 故障。随后在 schema 中加入文本、列表、推荐数、参数数和标量参数值上限，最终 live replay 结果如下。

有界 schema 的最终 replay 达标：

```text
25 passed, 0 failed, 0 skipped
pass_rate: 1.0
model_calls: 4
p95_latency_ms: 897.64
schema/route/recommendation/contrast/numeric/citation: 1.0
unapproved_job_creations: 0.0
cross_user_access_successes: 0.0
newly_passed: recommend_dem_001, recommend_gma_001
newly_failed: none
```

该 diff 使用上一轮 23/25 报告为 baseline，recommendation accuracy 与 schema validity 均提升 0.5；原始失败报告、修复后报告和 diff 保留在服务器 `eval-reports/`，没有覆盖或改写历史成绩。

## 换模型 replay

服务器使用同一套 offline harness、fixture tools 和 InMemory store，将模型从 Qwen3-14B-AWQ 切换为 Qwen3-8B-AWQ，并以 14B 有界 schema 达标报告为 baseline 生成机器可读 diff：

```text
model: Qwen3-8B-AWQ
25 passed, 0 failed, 0 skipped
pass_rate: 1.0
model_calls: 4
p95_latency_ms: 488.686
schema/route/recommendation/contrast/numeric/citation: 1.0
unapproved_job_creations: 0.0
cross_user_access_successes: 0.0
```

8B 与 14B baseline 均为 25/25，确定性指标无回归；8B 本次 P95 比 14B 的 897.64 ms 低 408.954 ms。原始报告和 diff 分别保留为服务器 `eval-reports/qwen3-8b-awq.json` 与 `eval-reports/qwen3-14b-to-8b.diff.json`。该成绩来自真实 vLLM 0.8.5 endpoint 和 Qwen3-8B-AWQ，不代表通过硬编码或 stub 伪造 live 成绩。

## Production replay

服务器使用普通 `omics_app` runtime DSN、真实成功 GMA job `8e39b468-e6ca-4948-8dee-a570382b85e6` 和与 owner 不同的随机 session 用户完成 production replay：

```text
25 passed, 0 failed, 0 skipped
pass_rate: 1.0
model_calls: 4
p95_latency_ms: 778.56
schema/route/recommendation/contrast/numeric/citation: 1.0
unapproved_job_creations: 0.0
cross_user_access_successes: 0.0
ground_cross_user_404_001: passed (271.61 ms)
```

production case 通过真实 `PostgresJobRepository.get_for_user(job_id, user_id)` 验证 ownership；请求用户与 job owner 不同，结果为 404，且没有进入 artifact 读取。

## R6 手工演示

服务器停止 `omicsprism-vllm` 后，通过部署 API `http://111.170.173.174:18086` 完成原手工工作流验证：

```text
backend health: HTTP 200
manual job: 6a071e89-9b46-4605-bcf6-79260a482c26
progress/files/images: HTTP 200
OmicsPrism_results.zip: HTTP 206 (browser Range download)
```

随后 vLLM 容器恢复为 `running`、exit code 0；`127.0.0.1:18000/health` 成功，`/v1/models` 返回 `Qwen3-14B-AWQ`。当前 shell DSN 查询不到手工 job，是因为它指向 production eval 测试库而非部署 API 的 job store；该 SQL 不作为 R6 证据。R6 结论：模型服务停止不影响原手工表单、任务进度、结果文件和下载路径。

## 验证记录

1. [完成] unit eval 在无外部服务时执行全部 25 case。
2. [完成] Qwen3-14B-AWQ offline replay 达到 25/25，并保留从失败 baseline 到达标报告的机器可读 diff。
3. [完成] 用 `omics_app` DSN、真实他人 job id 和随机请求 session 跑 production，跨用户 case 为 404。
4. [完成] 关闭 vLLM，通过原手工流程完成任务与结果访问，确认 R6；随后恢复 Qwen3-14B-AWQ。
5. [完成] prompt/schema 变更后 replay 与 diff 已审阅；Qwen3-8B-AWQ 第二模型 replay 为 25/25，并生成相对 14B baseline 的机器可读 diff。

## 已知缺口

- live model eval 依赖服务器 GPU 与显式 endpoint，只作为人工或定时回归运行，不进入普通 CI；endpoint 缺失时 harness 明确 skip。
- production replay 只应在受控服务器使用准备好的跨用户 fixture job；不能对真实用户数据做开放式探索。
- 管理 UI、通用 RAG/SQL/shell、新业务工具和独立多-agent 服务均不在 Phase 5 范围内。
