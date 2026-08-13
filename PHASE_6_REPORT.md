# Phase 6 报告：Copilot 产品接入

> 状态：已完成。2026-08-13，Gate A-E 的代码、自动化回归、真实双服务器演示与人工红线审阅全部通过。后续工作切回维护与回归，不在本报告内扩展 Phase 7 能力。

## 1. 完成范围

- 以 PostgreSQL 持久化 thread、message、turn、plan、approval 与暂存输入，并以最小权限角色约束 append-only 和 ownership。
- 将既有 router、model、policy、tools、approval、grounding 与 verifier 组装为有界生产协调器，独立 agent worker 通过 lease、乐观锁和幂等键执行 turn。
- 提供 Cookie 身份下的 `/api/agent/*` HTTP/SSE 契约；模型只在算力服务器 agent worker 中调用，不进入 API 或 analysis worker。
- 提供 `/copilot` 对话、CSV 上传、结构化 plan 审批、实时 job 卡片、结果跳转和 evidence citation，并保留原手工分析入口。
- 固化双服务器部署：云服务器运行 nginx/API/PostgreSQL/Redis/MinIO，算力服务器运行 analysis worker、agent worker 与 vLLM。

## 2. Gate A-E 结论

| Gate | 交付 | 验收结论 |
| --- | --- | --- |
| A | 产品表、数据契约、ownership、审批和最小数据库权限 | 真实 PostgreSQL 权限测试通过；R3/R4/append-only 人工审阅通过 |
| B | `ProductionRunCoordinator`、输入来源、agent worker、lease/recovery、grounding | 服务器 repository/worker 测试通过；R1/R2/R4/R5/worker trace 人工审阅通过 |
| C | thread/message/turn/bundle/approval HTTP API 与 SSE | 服务器 API 与完整 backend 回归通过；R3/R4/R6/原子入队/SSE 人工审阅通过 |
| D | `/copilot` 前端、typed blocks、审批、job 进度、citation 与上下文 | 桌面/移动端自动化流程及真实 Qwen3 对话、审批、提交、解读人工复验通过 |
| E | 双服务器生产部署及四项真实演示 | analyze/approve/job、interpret/citation、跨用户 404、模型停机隔离全部通过 |

各 Gate 的完整命令、首次失败、修复过程和原始输出分别见 `PHASE_6A_REPORT.md` 至 `PHASE_6E_REPORT.md`。

## 3. 最终验证基线

最新本地回归：

```text
python -m pytest backend/tests -q -rs
158 passed, 6 skipped

python -m scripts.run_agent_eval --assembly unit
25 passed / 25 total

python -m compileall -q backend/app backend/agent_worker.py scripts
exit 0

npm test --prefix frontend
10 passed

npm run build --prefix frontend
passed; main entry 206.06 kB, CopilotPage 26.62 kB
```

6 个 skip 需要专用 PostgreSQL 测试环境。Gate A-C 已在服务器测试库执行真实权限、repository 和 API 用例，最终服务器完整 backend 回归曾达到 `144 passed`；skip 不在本报告中伪记为本地通过。

Gate E 的真实生产输出摘要：

```text
thread=404
job=404
bundle=404
approval=404

jobs_api=200  # vLLM 与 agent worker 停止期间
Qwen3-14B-AWQ, max_model_len=8192  # 恢复后
omicsprism-agent-agent-worker-1  Up
```

真实 DEG 流程在审批前没有创建 job，结构化批准后只创建一个 differential job，analysis worker 最终执行成功。已有结果解读的数字、row id 与 citation 来自本轮有界 evidence。模型组件停止期间，原手工分析、进度、结果页和下载继续可用。

## 4. R1-R6 最终结论

- [x] R1：模型只接收最小上下文并返回受校验决策，不持有 DB、shell、凭据、路径或原始 CSV。
- [x] R2：工具按 profile 白名单装配；interpretation profile 结构上没有写工具。
- [x] R3：身份由服务端 Cookie 注入，repository 绑定 resource id 与 user id；跨用户资源统一 404。
- [x] R4：写工具校验 owner、有效审批、TTL 与 plan hash；审批前 0 job，批准后恰好 1 job。
- [x] R5：结果回答只使用当前 evidence adapter 行；数字与 citation 可核验，空结果使用固定模板。
- [x] R6：vLLM 与 agent worker 停止不影响原手工分析、任务进度、结果页或下载。

## 5. Phase 6 DoD

- [x] 生产协调器使用真实 model/tool/store，fixture 协调器仅用于测试。
- [x] migration 004/005、PostgreSQL stores 与 `omics_app` 最小权限完成并在服务器验证。
- [x] thread/message/turn/approval/input-bundle API、服务端身份和跨用户 404 完成。
- [x] turn 幂等、单 thread 单活动 turn、lease recovery、乐观锁和稳定错误码完成。
- [x] 暂存输入审批前不建 job；有效审批只建一个 job，重放不重复。
- [x] `/copilot` 的会话、附件、审批、job 状态、结果跳转和 evidence 引用完成。
- [x] 用户可见内容只走 typed message blocks；模型不控制 HTML、URL 或按钮。
- [x] 25-case eval、backend 回归、frontend test/build/e2e 无安全或功能回归。
- [x] 四项真实双服务器演示与 Gate A-E 人工红线审阅通过。
- [x] OpenAPI 类型、架构、部署、回滚和排障文档已纳入版本控制。

## 6. 已知缺口与维护边界

- 当前匿名 Cookie 是产品既有身份边界；OIDC/RBAC、组织空间和管理员能力属于后续独立 Phase。
- SSE 流式传输 turn/message 状态，不提供 token 级模型输出。
- interpretation evidence 按 12 行/12 KB 有界，以适配当前 8192-token Qwen3；扩大上下文必须重新执行 R5 与生产负载验证。
- 前端 React Router 6 的 2 个 moderate advisory 已按当前无 SSR 的使用方式接受；升级 7.x 需单独做兼容回归。
- PostgreSQL 与 Redis 之间没有事务 outbox；现阶段依赖确定性 job id、submitted ids 和现有 job 幂等收敛。
- 后续维护不得绕过六条红线；新增工具仍需 registry、profile 白名单、spec 与红线测试同步更新。
