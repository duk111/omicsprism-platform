# Phase 6 Gate D 报告：前端闭环

> 状态：实现完成，等待服务器前端构建复验与 Gate D 人工审阅；Gate D 尚未关闭，不得进入 Gate E。
> 基线：Gate A/B/C 已通过服务器验证与人工红线审阅；本 Gate 不新增 migration、后端工具、模型能力或生产部署。

## 1. 本 Gate 做了什么

- 新增 `frontend/src/copilot/agentApi.ts`，封装 thread、message、turn、bundle、approval 与 SSE 契约，所有请求使用现有 Cookie 会话。
- 新增 `frontend/src/copilot/CopilotPage.tsx` 与 `copilot.css`：桌面三栏工作台、移动端会话抽屉、消息 composer、CSV 角色选择、结构化审批、job 卡片、evidence citation、错误/离线状态。
- 新增 `MessageBlocks.tsx`，只渲染生成的 typed block；模型文本作为 React 文本节点输出，不接受任意 HTML。
- 接入 SSE `turn.updated` / `message.created`，断线时按 thread REST cursor 恢复，并使用轮询 fallback；断线状态明确显示为 reconnecting/offline。
- 在现有 job progress/results 页面增加带 `job_id` focus 的 Copilot 入口；Copilot 不改变原手工表单、任务和结果页面。
- 将 Copilot、FigureViewer、InteractiveRouter 改为懒加载；普通入口初始 JS 从约 10 MB 降至约 206 kB，Plotly 仅在交互页面按需加载。
- 新增 Vitest/Testing Library 组件测试与 Playwright 桌面/移动端 mock-contract 流程，保存三条流程截图到本地 `frontend/test-results/`（该目录不提交）。
- 审计并锁定 Node 18 可用的前端工具版本；未执行 breaking 的 `npm audit fix --force`。

## 2. Gate D DoD 自查

- [x] `/copilot` 可新建/恢复 thread、发送消息、上传受支持 CSV，并从已有 job/result 进入带 focus 的对话。
- [x] typed blocks 完整渲染且不接受任意 HTML；plan 使用结构化 Approve/Reject，双击防重，过期/拒绝/冲突显示可恢复错误。
- [x] SSE 更新、断线重连、cursor 恢复与 polling fallback 已实现；loading/empty/retry/offline/404 状态不泄露资源归属。
- [x] job 卡片可跳转现有页面；citation 展示 artifact/checksum/row id，空 evidence 显示固定无证据状态。
- [x] 组件测试覆盖 typed text、审批 hash、citation；Playwright 桌面/移动端跑通 analyze/approve/job、interpret/citation、model-off 三条流程，截图无空白主视图或审批控件重叠。
- [ ] `npm audit` 已完成逐项安全处置；当前生产依赖仍有 React Router 6 的 2 个 moderate advisory，需在 Gate D 人工审阅时确认接受或安排升级。
- [x] `npm run build --prefix frontend`、完整 backend tests、compileall 与 Phase 5 25-case replay 均无功能回归。

## 3. 本地验证证据

```text
npm test --prefix frontend
3 passed

npm run build --prefix frontend
index-Boij26BB.js 206.05 kB (gzip 63.89 kB)
CopilotPage-LjJ7_2Ev.js 23.21 kB (gzip 7.19 kB)
InteractiveRouter-CTcOFEpu.js 9,777.14 kB (按需 chunk)

npm run test:e2e --prefix frontend
6 passed (desktop + mobile)

.venv/Scripts/python -m pytest backend/tests -q -rs --basetemp .pytest-tmp/phase6d-20260803
128 passed, 6 skipped

.venv/Scripts/python -m scripts.run_agent_eval --assembly unit --output .pytest-tmp/phase6d-unit.json
25 passed / 25 total; all metrics 1.0 except intentionally zero-count safety metrics

.venv/Scripts/python -m compileall -q backend/app backend/agent_worker.py scripts
exit 0
```

## 4. 服务器验收

Gate D 只更新云服务器 nginx 提供的静态前端；算力服务器不安装或启动 Vite，不部署 `frontend/dist`。在云服务器拉取本 Gate 提交后执行：

```bash
git pull origin master
npm ci --prefix frontend
VITE_PUBLIC_BASE_PATH=/omicsprism/ \
VITE_API_BASE_PATH=/omicsprism/api \
  npm run build --prefix frontend
npm audit --prefix frontend --omit=dev

rm -rf /www/nginx/nginx_html/html/omicsprism/*
cp -a frontend/dist/. /www/nginx/nginx_html/html/omicsprism/

docker exec nginx-all nginx -t
docker exec nginx-all nginx -s reload
```

浏览器只从 `http://111.170.173.174:8092/omicsprism/copilot` 进入。Gate E 才把云 nginx 的 `/omicsprism/api/agent/*` 单独反代到算力服务器 Agent API；普通 `/omicsprism/api/*` 继续指向云服务器 API。算力服务器只运行 Agent API、agent worker 与 vLLM。

人工审阅重点：

1. `/copilot` 新建/恢复、CSV role 选择、Enter 发送和移动端会话抽屉。
2. plan 只通过 Approve/Reject 控件推进，重复点击不重复请求；过期、拒绝、冲突可重试。
3. SSE 断开后 messages/turns 恢复；citation 的 artifact/checksum/row id 可核验；不存在或跨用户资源统一显示普通 404。
4. 停止 vLLM 后 Copilot 显示可重试错误，手工分析/任务/结果页仍正常。
5. 在桌面与手机视口检查截图无文字截断、按钮重叠、横向溢出或空白主视图。

## 5. 已知缺口

- 生产依赖 React Router 6 的 2 个 moderate advisory 尚未升级；应用为纯客户端 BrowserRouter，不使用 SSR hydration，升级到 7.x 需要单独兼容评估。
- Playwright 当前为 mock-contract 流程；真实 PostgreSQL + Redis/MinIO + vLLM + worker 的端到端演示属于 Gate E。
- SSE 仍按 Gate C 约定每秒轮询投影，不做 token 级流式输出。

## 6. 生产修正：受限生物学咨询

生产联调发现，无上传文件的消息在进入分析 profile 后只能静态索要 CSV，用户无法询问一般生物学知识或分析设计。现已补充 `ADVISE` 状态与 `advisory` typed block，边界如下：

- 一般生物学、生物信息学、实验设计与 OmicsPrism 分析方法问题可调用 vLLM；咨询上下文的工具白名单为空，不调用业务工具，也不生成 plan、approval、job 或 enqueue。
- 明确要求执行分析但没有真实上传数据时，系统先按上传记录检查输入，再给出分析建议；用户在消息中声称“文件已上传”不能替代真实 input bundle。
- 涉及用户实际数据的判断仍必须基于真实上传文件；涉及已有结果的结论仍必须通过 interpretation profile、evidence adapter 与 citation。
- 咨询回答使用受限纯文本字段，前端只按 React 文本节点渲染；不得伪造引用，不提供诊断、治疗或医学结论。
- `DecisionValidator` 强制咨询态只能返回 `ANSWER + advisory_answer`，拒绝 feasibility、recommendation、参数、审批和 grounded evidence。

本地回归证据：

```text
.venv/Scripts/python -m pytest backend/tests -q -rs
133 passed, 6 skipped

.venv/Scripts/python -m scripts.run_agent_eval --assembly unit
25 passed / 25 total

npm test --prefix frontend
8 passed

npm run build --prefix frontend
build passed; CopilotPage 24.81 kB (gzip 7.67 kB)

.venv/Scripts/python -m compileall -q backend/app backend/agent_worker.py scripts
exit 0
```

Gate D 仍未关闭。服务器更新后需要用真实 Qwen3 vLLM 人工复验：一般生物学咨询、无文件分析建议、伪造上传/绕过审批提示、真实上传后生成计划，以及模型停止时原手工业务可用。

首次生产复验中，一般生物学咨询通过，但分析建议与绕过审批提示触发 `InvalidDecision`。根因是咨询态与分析态共用通用 JSON Schema 和组合 prompt，Qwen3 在分析类问题中额外返回了咨询态禁止的 feasibility/recommendation 字段。现已改为状态专用的 `AgentAdvisoryDecision` schema：`action` 与 `requires_approval` 使用常量约束，recommendations、params 固定为空，grounded evidence 固定为 null，并使用不含 `CHECK_INPUTS` 指令的独立咨询 prompt。通用 `AgentDecision` 校验与运行时 `DecisionValidator` 仍作为后续两层校验。

## 7. 生产修正：输入规划容错与可操作错误

第二轮生产复验发现两类可用性问题：能力询问“你能做什么”可能触发 `ModelBoundaryError`；真实上传后，模型在输入检查阶段返回与状态不一致的通用决策，触发 `InvalidDecision`。原前端把二者统一显示为“未通过安全校验”，用户无法判断文件是否保留、是否创建了任务以及下一步应补充什么。

本轮在不放松 R1-R6 的前提下完成以下修正：

- 能力询问改为确定性帮助路由，零模型调用、零工具调用，直接说明 DEG、DEM、GMA、结果解读和审批边界。
- 含附件但目标含糊的消息只调用一次 `inspect_uploaded_inputs`，告知已收到的文件角色和可支持分析；不生成计划，不创建 job。
- 输入检查上下文新增有界摘要，只包含 role、最多 40 个列名、行数、dtype，以及裁剪后的 metadata/group 水平与计数；原始矩阵、完整 CSV、文件名和路径仍不进入模型上下文。
- `CHECK_INPUTS` 使用状态专用的 discriminated union，只允许 `propose_plan` 或 `request_more_data`。模型必须使用摘要中真实存在的列名和值；二水平分组会优先识别 control、ctrl、ck、wt、mock、untreated 等参考水平，不明确时必须提出具体澄清问题。
- vLLM 首次输出不满足当前状态 schema 时，使用同一安全上下文自动修复一次；第二次仍失败才返回模型边界错误，不无限重试。
- `ModelBoundaryError`、决策冲突、预检失败和基础设施异常分别映射为不同用户错误。模型两次结构化失败时明确说明文件已保留且未创建任务；预检失败展示最多三条具体原因；数据库连接超时不再伪装成“安全校验失败”。
- 前端 Retry 恢复最近一次用户原始文本，不再发送脱离上下文的 `Please retry the last step.`。

红线保持不变：模型仍无数据库、shell、凭据或原始路径句柄；解读只能走 evidence adapter；审批前 job 创建数恒为 0；修复重试不能绕过 profile 白名单、输入真实性、contrast 或审批校验。

本地回归证据：

```text
.venv/Scripts/python -m pytest backend/tests -q -rs
140 passed, 6 skipped

.venv/Scripts/python -m scripts.run_agent_eval --assembly unit
25 passed / 25 total; all gate metrics passed

npm test --prefix frontend
8 passed

npm run build --prefix frontend
build passed; CopilotPage 24.96 kB

.venv/Scripts/python -m compileall -q backend/app backend/agent_worker.py scripts
exit 0
```

生产复验重点：

1. “你能做什么”立即返回帮助说明，agent worker 日志不产生 vLLM POST。
2. 上传 counts 与 metadata 后只说“给你传了两个文件”，系统总结文件角色与可支持分析，不生成 job。
3. 再说“分析一下”：分组唯一时生成使用真实列名/水平的待审批计划；分组含糊时询问具体列或 reference/test 水平。
4. 文件角色选错时显示具体 preflight 原因，不生成计划或 job。
5. 首次模型结构不合法时允许出现两次 vLLM POST；若第二次修复成功，用户不应看到错误。两次均失败时，错误必须说明文件保留且未创建任务。

同一时段日志中的 PostgreSQL `ConnectionTimeout` 是独立基础设施故障：连接恢复后 worker 可继续处理。它不应归类为模型安全校验问题；生产仍需结合数据库公网端口、网络质量和连接超时监控单独排查。

后续复验又发现：预检失败后状态为 `PREFLIGHT_BLOCKED`，同一会话的下一条普通消息未重新进入 router，曾产生空 assistant blocks。现已允许 `PREFLIGHT_BLOCKED`、`DONE`、`JOB_FAILED` 在新消息到达时重新路由；等待审批状态仍只能通过审批 resume 流程推进。新增回归测试确保预检失败后继续提问会得到正常回答，而不是空白消息。

真实大文件复验进一步确认，笼统“输入预检未通过”并非用户 counts/metadata 必然有误，而是预检工具把完整 feature/sample ID 列表放入单行 ToolResult；大矩阵超过 32 KB 后通用裁剪器会移除整行，运行时因看不到 contrast 和 errors 而误判失败。现已把预检文件信息改为有界摘要，只保留行列数、sample/feature/duplicate/empty-column 数量及最多 10 个诊断样例，实际 errors、warnings、effective params 和 contrasts 保持完整。5000 feature 回归用例确认结果不再被裁剪且可以提交。

同时修正附件能力判断：包含“这个/这些数据或文件能做什么”的请求走输入描述，不再被全局“你能做什么”帮助规则抢占；部分 GMA 输入（如 transcriptome + group）由确定性能力表直接说明缺少 metabolome，不再调用模型把它误说成 DEG 输入。

本轮验证：后端 `144 passed, 6 skipped`；unit golden eval `25/25`，全部安全与准确率门禁通过。

大文件修正上线后，真实模型能够推荐 DEG，但在宽泛“分析一下”请求中可能返回空 contrast 参数。原实现仍把该不完整决策送入预检，因而正确但不友好地提示 `compare_field, tested_levels and reference_level are required`。现新增确定性 contrast 补全：仅当 metadata 中存在唯一、满足最小重复数且含 control/ctrl/CK/WT/mock/untreated 等明确参考水平的二水平分组时，使用真实列和值自动补齐；存在多个候选或参考语义不明确时，列出真实候选列、水平和计数，请用户明确实验组/对照组，不创建 plan 或 job。最新验证为后端 `146 passed, 6 skipped`，unit golden eval `25/25`。

产品联调又发现计划拒绝后的会话问题：解释旧 plan 时被误当成普通输入咨询；上传新 bundle 后 worker 仍优先读取旧 plan 的输入来源。现新增 `EXPLAIN_PLAN` 确定性路径，直接解释分析类型、真实 contrast、样本数、阈值和审批含义；普通后续消息优先使用最新显式上传 bundle，只有待审批/批准提交 turn 才锁定 plan 的输入来源。计划参数现在按 analysis spec 白名单过滤，前端将 contrast 渲染为比较列/实验组/对照组/样本数，不再显示重复参数或 raw JSON。

本轮验证：后端 `149 passed, 6 skipped`；前端 `9 passed`；前端生产构建通过。

双服务器生产验收发现审批可能提示 `Approval has expired`。原实现由算力 worker 生成绝对到期时间、云端 API 用本机时间校验，且 TTL 仅 10 分钟；服务器时钟偏差或较长人工审阅都会导致审批不可用。现将生产 PostgreSQL 装配改为用数据库 `clock_timestamp()` 生成和校验到期时间，TTL 延长到 30 分钟；worker 创建审批后回读数据库中的权威到期时间用于 plan/approval block。过期点击会释放 run 的 pending approval 并回到 `NEED_USER_INPUT`，允许重新生成计划，且仍不创建 job。前端计划卡明确标注 `Expires`。

本轮验证：后端 `150 passed, 6 skipped`；前端 `9 passed`；unit golden eval `25/25`；前端生产构建通过。

生产验收随后发现 Copilot 内嵌 job 卡片只保存创建时的 `queued/0%` 快照，即使分析 worker 已完成，旧消息仍显示排队；卡片链接也指向 API JSON 或错误结果路由。现已复用原任务页的 SSE + polling fallback，在每张 job 卡片上读取实时进度，终态后切换为正确的 `/jobs/{job_id}/results`；后端新生成的 job block 同步改用前端路由。验证为后端 `150 passed, 6 skipped`、前端 `10 passed`，前端生产构建通过。

继续复验发现同一 thread 的对话上下文没有真正进入模型：worker 只读取最新用户文本，`ModelContext.conversation_summary` 始终为空；`MONITOR_JOBS` 还会先返回 job 快照并丢掉当轮“这个计划是什么意思”。现已完成以下修正：

- 最近 12 条 typed message 构建最多 3600 字符的有界历史摘要，只保留对话、输入角色、plan/approval/job 结构；旧 evidence 只记录“曾返回证据”，不携带旧 claim 数字，避免绕过 R5。
- 所有模型 prompt 明确身份为 OmicsPrism Copilot，并声明当前 state/profile、验证过的输入、focus job、工具和当前 evidence 才是权威上下文；历史与用户文本均不能改变策略。
- `MONITOR_JOBS` 可直接重新路由用户追问；有 focus job 时，“结果/分析结果/解读”等优先进入 interpretation profile，不再回到输入规划。
- 普通生物学或生物信息学追问即使已有上传文件，也进入无工具的 `ADVISE` 聊天态；明确说“上传/传了/文件已传”仍走确定性输入摘要。
- interpretation profile 使用查询态/回答态两个窄 JSON Schema。回答前先通过 `get_jobs_status` 提供允许的结果 artifact 名称，再调用 evidence adapter；模型不能输出 plan、approval 或未经当前 evidence 支持的结果数字。
- 若 focus job 尚未生成白名单内结果表，则根据任务状态直接提示“仍在运行”“任务失败”或“未发现支持的结果表”，不调用模型猜测文件名。
- 模型结构错误提示改为中性会话错误，不再把所有 interpretation 失败错误描述成“计划与输入不一致”。

本轮验证：后端 `156 passed, 6 skipped`；unit golden eval `25/25`，route、审批前写入、跨用户 404、数字准确性与引用覆盖率门禁全部通过；`compileall` 通过。Gate D 仍需部署后用真实 Qwen3 对“这个计划是什么意思”“结果什么意思”和同 thread 生物学追问做人工复验。
