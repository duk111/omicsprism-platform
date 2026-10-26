# PR 10：结果页 Copilot 融合交付记录

日期：2026-10-26。审计基线：`302075a`（PR 9）。

DEG、DEM、GMA 现有结果页均可直接打开右侧 Copilot。聊天复用原组件、原 API、原 SSE、原 LangGraph 和结果问答工具；新增的是面板显示模式及四字段的可选视图引用，没有新增 Agent、聊天 API、数据库迁移或科学计算流程。

## 阶段 A：实际仓库审计

| 问题 | 实际实现及最小复用方案 |
| --- | --- |
| 1. Result 如何取得任务和分析类型？ | `App.tsx` 的 `JobResultsRoute` 从 `/jobs/:jobId/results` 读取参数；共用 `ResultsPage` 从 `/api/jobs/{jobId}` 取得 `analysis_type`，并加载 files/images。三种分析没有三套独立结果页。 |
| 2. 原有 Copilot 入口？ | 进度页、结果页均有 `/copilot?job_id=...` 链接，顶栏有普通 Copilot 入口。本次只将结果页入口改为面板，并给 `FigureViewer` 增加同样入口；进度页链接保留。 |
| 3. UI 能否嵌入？ | 可以。`MessageBlocks`、`GraphInterruptPanel`、`TracePanel` 已独立；`CopilotPage` 增加 `embedded/context/onClose` 参数即可复用。面板隐藏会话列表和运维上下文栏，聊天主体保持共用。 |
| 4. SSE、状态和会话耦合程度？ | `agentApi.ts` 已封装 HTTP；EventSource、恢复、消息/turn 状态和发送逻辑集中于 `CopilotPage`。不再拆出第二套状态管理，内部共用 `CopilotConversation`，按 Job key 隔离实例。 |
| 5. Agent 是否已有 Artifact 上下文？ | 已有 `FactIndex.job_artifacts`、Result QA 上下文、`describe_artifacts`、`query_artifact/query_result`、带 checksum/row_ids 的证据及 `ResultSelection`。缺少的是当前 UI 视图引用。 |
| 6. PR 9 机制能否复用？ | 能。`runtime._merge_turn_input` 已处理 `focus_job_ids` 切换，清理旧实体、引用、结果选择并隔离模型历史；本次只补充视图变化时清理旧查询选择。UI 的完整聊天记录保留。 |
| 7. 哪些 React 组件复用？ | 共用 `ResultsPage`、`FigureViewer`、`CopilotPage` 聊天主体、`MessageBlocks`、`GraphInterruptPanel`、现有输入框/附件/错误/任务状态组件。图表路由和各 `Interactive*` 图表组件无需改写。 |
| 8. 哪些无需后端修改？ | 入口、开关、并排/移动端覆盖布局、可见任务上下文、示例问题和 Job 会话隔离均可纯前端完成。为了安全地传递当前 Artifact，仅在原 turn 请求和既有状态/投影中增加可选 `result_view`。 |

PR 7 的实际能力包括 `grounding.py`、`verifier.py`、`nodes/main.py` 和 `nodes/result_qa.py` 中的证据校验。PR 8 已有确定性过滤、排序、限制及字段/实体查询。PR 9 已有意图路由、多轮查询选择重查和任务切换机制。本次复用这些能力，相关专项回归通过。

审计也发现两个既有问题：用户读取 Job 的共享入口未统一拒绝软删除（本次修复）；完整 Agent 评估有一个基线失败（下文列明，未改变评估期望来使其通过）。

## 实现与交互约定

- 打开面板会创建绑定当前 Job 的会话和 SSE 连接，不发送模型 turn。建议问题只填入输入框。
- 面板明确展示分析类型、完整 Job ID、当前结果名称和已有 Artifact 路径。
- 选择器支持结果总览、已有图表及 CSV 结果表。原页面的结果表仍是下载列表，没有新增表格预览系统。
- 同一 Job 切换图表/表格，聊天和会话保持；下一次提问发送最新视图。视图变化清除旧查询对象、实体和 last citation，避免沿用先前表格选择。
- 选中表格时，两条现有查询入口都限制证据查询使用该表。需要查询其他表时，选择对应表格或切回总览。
- 图表引用不等于数据表映射；模型必须通过已有工具发现来源。不支持从图表标题得出科研结论，来源不明确时应澄清。
- Job A 切到 Job B 时卸载旧会话与 SSE，面板默认关闭；在 B 打开创建独立会话。不会将 A 的聊天记录展示为 B 的会话。
- 关闭再打开保留本页会话和草稿；刷新、导航离开或更换 Job 后，重新打开新建会话。历史会话继续保存在普通 Copilot 页面；面板明确说明此行为。
- 宽屏保留结果区与 472px 面板并排；1100px 以下采用覆盖面板，720px 以下适配移动顶栏。图表查看器与面板可在宽屏并排，输入科研数值不会触发查看器快捷键。
- 无 Job 的 `/copilot` 保留原普通聊天和会话恢复行为；`/copilot?job_id=...` 仍可使用。原会话列表选中其他会话时以所选会话的真实焦点为准。

## 上下文、权限与证据验证

每次提问继续 POST 原 `/api/agent/threads/{id}/turns`，例如：

```json
{
  "message": "当前结果中 padj 最小的基因是什么？",
  "focus_job_ids": ["pr10-deg"],
  "result_view": {
    "job_id": "pr10-deg",
    "analysis_type": "DEG",
    "result_type": "table",
    "artifact": "outputs/deg.all.csv"
  }
}
```

`message` 保持用户原文，Job ID 没有藏入 prompt。后端在接收请求和执行队列任务时分别检查：单任务焦点、Job 所有权、Job 未删除、分析类型与服务端一致、Artifact 路径与该 Job 注册清单完全匹配。视图验证不打开任何客户端路径，不接受任意绝对路径、目录穿越或未注册文件。

`ContextAssembler.assemble_for_result_qa` 仅投影匹配当前任务的视图。前端提供的名称不进入科研证据，最终回答继续经过原工具、GroundedAnswerPipeline 和 AnswerVerifier。`JobStorageService.get_for_user` 现在统一拒绝软删除；内部任务读取入口没有修改。

测试覆盖了跨用户、已删除、不存在、入队后所有权改变、Artifact 移除、伪造路径、分析类型不符、视图/焦点不一致，以及模型尝试查询另一张表。均无法产出越界证据。

## 测试结果

| 验证 | 结果 |
| --- | --- |
| Vitest：`npm test -- --reporter=dot` | 28 passed（新增 7 项，含三种分析入口、传参、切换、重开、普通聊天、API/SSE 错误与轮询恢复） |
| TypeScript + Vite：`npm run build` | 通过；已有大型 Plotly 资源包警告仍存在 |
| 原 Playwright：`npm run test:e2e` | 6 passed，桌面和移动端各 3 项 |
| 结果页真实 API Playwright：`npm run test:e2e -- --config playwright.pr10.config.ts` | 10 passed，桌面和移动端各 5 项 |
| 后端全量：`.venv/Scripts/python.exe -m pytest backend/tests -q --tb=short --basetemp=.tmp/pr10-final-backend` | 545 passed、14 skipped、1 xfailed、1 个已复现的基线失败 |
| PR 7/8/9 + 新视图、证据、Artifact、权限专项回归 | 177 passed、1 skipped |

唯一全量失败为 `test_agent_eval_v2.py::test_evaluator_self_tests_keep_malformed_evidence_out_of_agent_quality_metrics`：capability pass@1 实际为 `0.6666666666666666`，测试期望 `1`。已用独立 detached worktree 在未修改的 `302075a` 上运行同一测试，得到完全相同的失败。该问题不由本次修改引入，需后续检查既有 capability 评估用例与 PR 9 路由/角色边界的匹配关系。

14 项跳过与 1 个预期失败保留现有测试条件，本次没有降低或放宽测试断言。真实 LLM、PostgreSQL/Redis 外部服务链路未在此次浏览器验收中验证。

## 真实 Artifact 端到端证据

`backend/tests/pr10_server.py` 只用于测试，以临时目录、内存 product store/checkpointer/queue 和已有 Scripted Model 启动生产 Agent API、Job API、LangGraph、工具与证据管线。禁止启动科学分析任务。

浏览器访问 DEG 结果页 → 打开 Copilot → 询问当前任务中 padj 最小的基因 → 原 SSE 接收证据回答。执行观察确认工具调用为：

```json
{"tool":"query_artifact","arguments":{"job_id":"pr10-deg","artifact":"outputs/deg.all.csv","sort":"padj asc","limit":1}}
```

结果来自已有 `backend/app/agent/fixtures/result_query/deg.all.csv` 的真实 Worker 捕获字节及 provenance manifest：第 1 行 `gene_up`，`padj=4.335429232883268e-101`，checksum `30ccd0253291ef4661c6ba815f9d2b295ed454d5c7826f0601b5f168aea0ede2`。回答显示正确 Artifact、Rows 1 和 checksum。

同一端到端场景还验证图表→结果表切换保留历史、最新表格引用随下一次提问发送、关闭重开、刷新新建会话和图表快捷键隔离。DEG、DEM、GMA 的打开与上下文展示在两种屏幕均通过；DEM/GMA 科研证据查询继续由既有真实 Artifact 后端回归覆盖。

**模型为 Scripted Model；真实 LLM 验证尚未完成。** 截图中的 SVG 是明确标记的 UI fixture，用于验证图表与面板并排，科研答案来自真实 CSV Artifact，不从该示意 SVG 推导。

## UI 截图

- [桌面：结果与证据回答](desktop-deg-verified-answer.png)
- [桌面：图表查看器与 Copilot 并排](desktop-deg-figure-and-copilot.png)
- [移动端：证据回答](mobile-deg-verified-answer.png)
- [移动端：图表上的覆盖面板](mobile-deg-figure-and-copilot.png)
- [DEG 桌面入口](desktop-deg-panel.png) / [移动入口](mobile-deg-panel.png)
- [DEM 桌面入口](desktop-dem-panel.png) / [移动入口](mobile-dem-panel.png)
- [GMA 桌面入口](desktop-gma-panel.png) / [移动入口](mobile-gma-panel.png)

截图已实际打开检查，结果/聊天并排和移动端输入区可用。

## 修改文件清单与 Git Diff 摘要

| 文件 | 修改目的 |
| --- | --- |
| `frontend/src/App.tsx` | 结果页面板、当前结果选择器、任务变化隔离 |
| `frontend/src/copilot/CopilotPage.tsx` | 共用聊天的嵌入模式、可见上下文、建议问题、SSE 异常恢复与旧请求隔离 |
| `frontend/src/copilot/result-copilot.css` | 并排/覆盖布局与面板样式 |
| `frontend/src/copilot/agentApi.ts` | 在原 turn 请求中发送可选视图引用 |
| `frontend/src/api-types.ts` | 与后端一致的四字段视图引用及可选请求字段 |
| `frontend/src/viewer/FigureViewer.tsx` | 图表入口及聊天输入快捷键隔离 |
| `backend/app/agent/schemas.py` | 有界 `ResultViewReference` |
| `backend/app/agent/view_context.py` | Job/Artifact 引用验证与所选表查询边界 |
| `backend/app/agent/api.py` | 入队前验证视图 |
| `backend/app/agent/queue.py` | 原队列输入携带可选视图 |
| `backend/app/agent/graph.py` | 既有 State 增加可选引用，未改变图结构 |
| `backend/app/agent/runtime.py` | 执行时复验、视图变化清理旧查询对象 |
| `backend/app/agent/context.py` | Result QA 的最小上下文投影 |
| `backend/app/agent/model.py` | 声明视图引用不等于证据及图表来源歧义处理 |
| `backend/app/agent/nodes/main.py` | 原 query_artifact 路径校验所选表边界 |
| `backend/app/agent/nodes/result_qa.py` | 原 query_result 路径校验所选表边界 |
| `backend/app/job_store.py` | 用户读取统一拒绝软删除 Job |
| `frontend/src/copilot/ResultCopilot.test.tsx` | Vitest 交互与传参回归 |
| `backend/tests/test_agent_result_view.py` | 上下文、权限、切换、双查询入口回归 |
| `backend/tests/pr10_server.py` | 临时存储的真实 API/Scripted Model 浏览器验收服务 |
| `frontend/e2e/result-copilot.spec.ts` | 真实 API、Artifact、SSE、桌面/移动验收 |
| `frontend/playwright.pr10.config.ts` | 自动启动独立测试后端及前端 |
| `frontend/playwright.config.ts` | 原 UI mock 测试与需后端的验收配置分开运行 |
| `reports/pr10/implementation.md` 与本目录 10 张 PNG | 审计、交付说明与截图证据 |

生产修改集中在上述 17 个文件。没有修改科学引擎、任务参数/执行系统、图表计算、已有结果查询协议或 Grounding/Verifier 实现；原查询工具仅增加所选表的引用边界。没有新增 UI 框架、依赖、迁移或自动分析编排。

## 尚未完成与建议

1. 在可用的真实模型服务上重复 DEG 最小 padj 问答，并抽查 DEM/GMA 的实际提问效果，尤其是图表与来源表不明确时的澄清。
2. 单独修复基线 capability 评估失败；保留本报告中的基线复现事实，不把全量测试描述为全绿。
3. 本版选择结果表只切换引用，仍复用原下载交互；表格预览和可靠的图表→数据表映射可按后续需求分别实现。
