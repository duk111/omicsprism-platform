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

服务器需要在拉取本 Gate 提交后执行：

```bash
git pull origin master
npm ci --prefix frontend
npm run build --prefix frontend
npm audit --prefix frontend --omit=dev
```

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
