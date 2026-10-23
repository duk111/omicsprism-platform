# OmicsPrism 全链路端到端回归验收报告

日期：2026-10-22（Asia/Shanghai）  
结论：**BLOCKED，未满足发布验收条件**。三种分析的真实科学引擎 Worker 执行均通过，且已在本地真实 Redis 上完成普通 API 到 Worker 的 DEG、DEM、GMA 顺序链路；但本地 JSON 存储在 API/Worker 并发写入时出现 Job 状态竞态，Copilot 使用真实运行产物的查询没有完成验证，前端 Vitest 与 Playwright 已修复并通过，但 Copilot 真实模型与 artifact 查询仍未完成。

## 1. 测试环境

| 项目 | 版本 / 配置 |
|---|---|
| Platform commit | `d7a2dbb5a5e4ec92e58e414d884eb7e43419dc77`（任务发布与失败恢复可靠性） |
| OmicsPrism core commit | `a17ee2291789cdc0ee2db62baa0713106d521a12` |
| OmicsPrism 安装版本 | `0.5.0`，Platform `.venv` 中导入路径指向工作区 `omicsprism/src/omicsprism` |
| Python / Node / npm | Python `3.13.7` / Node `v18.19.0` / npm `10.2.3` |
| 关键计算依赖 | FastAPI `0.136.1`、Pydantic `2.13.4`、pandas `2.3.3`、NumPy `2.4.4`、SciPy `1.17.1`、scikit-learn `1.8.0`、PyDESeq2 `0.5.4`、AnnData `0.12.14`、XGBoost `3.2.0` |
| 关键平台依赖 | pytest `9.1.1`、Redis 客户端 `8.1.0`、psycopg `3.3.4`、LangGraph `1.2.11`、Vitest `1.6.1`、Playwright `1.48.2` |
| DB / 队列 / 文件服务 | 本机 PostgreSQL 18（数据库 `omicsprism`，`omics_app` 可连接）、Redis 3.0.504（RESP2，6379）可用；MinIO 未运行。真实服务测试使用本地文件存储 + 真实 Redis，并分别验证了 JSON 与 PostgreSQL Job repository。 |
| 测试数据 | 新集成测试运行时生成 6 样本 DEG Counts/Metadata、含缺失值的 6 样本 DEM 矩阵，以及 8 样本 GMA transcriptome/metabolome/group CSV；存放在 pytest 临时目录。现有前端示例数据也存在，但没有作为大规模数据参与新测试。 |

复现后端测试时，在 Windows 将 `TEMP`、`TMP` 指向可写目录后运行：

```powershell
$env:TEMP = '.test-tmp'; $env:TMP = '.test-tmp'
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```

## 2. 测试结果

### DEG / DEM / GMA

| 测试名称 | 类型 | 真实计算 | 结果 | 证据 |
|---|---|---:|---|---|
| `test_worker_executes_real_deg_pipeline_and_writes_results` | 科学引擎集成 | 是，PyDESeq2 实际运行 | PASS | `backend/tests/test_scientific_engine_integration.py`；pytest 临时目录下的 `*.all.csv`、`plots/volcano/`。覆盖 Preflight、Worker pipeline、必要结果列和严格 JSON 解析。 |
| `test_worker_executes_real_dem_pipeline_with_missing_values` | 科学引擎集成 | 是，真实 DEM pipeline | PASS | 同上；临时目录 `*.all.csv` 检查 `metabolite_id/vip/log2FoldChange/pvalue/padj_bh/dem_status` 及有限统计值。 |
| `test_worker_executes_real_gma_pipeline_and_writes_network_tables` | 科学引擎集成 | 是，真实 load/preprocess/关联建模/报告 | PASS | 同上；检查 T02 网络表 schema、数值有限、生成 PNG、所有 JSON 严格解析；GMA Preflight 不需要 DEG contrast。 |
| OmicsPrism 核心仓库独立 pytest | 单元/集成 | 未运行 | SKIPPED | 核心仓库当前没有 `tests/` 测试目录；本轮由 Platform 调用安装的 core pipeline。 |

DEG 的极小数据运行产生 PyDESeq2 dispersion trend 回退及 NumPy runtime warnings，但管线成功导出结果。该小数据用于执行和产物契约回归，不构成真实生物学效应的参考验证。

### Agent / 参数契约

| 测试名称 | 类型 | 真实计算 | 结果 | 证据 |
|---|---|---:|---|---|
| Agent 参数解析、零值/False、GMA 参数修改契约（`test_job_execution_params.py`） | 单元 | 否，pipeline mock | PASS | Platform pytest；检查支持参数映射、默认值、GMA 不接受 DEG 专用配置。 |
| GMA 参数重复/连续多轮 Agent 编辑的完整真实 Worker 执行 | E2E | 否 | BLOCKED | 有参数解析及 Agent 图流程测试，但本轮未覆盖多轮 Agent 修改后确认、落库并执行真实 pipeline 的完整序列。 |
| Copilot 分析确认流程（Playwright） | Mock E2E | 否 | PASS（2/2 viewport） | `frontend/e2e/copilot.spec.ts`；UI 请求由 Playwright mock 返回 Job 卡片，不运行 API 或引擎。 |
| Vitest 全套（21 tests） | 组件/客户端单元 | 否 | PASS（21 passed） | 补齐 `agentApi.saveFeedback` 客户端封装后，`npm test -- --reporter=dot` 全绿。 |

### Job / 队列

| 测试名称 | 类型 | 真实计算 | 结果 | 证据 |
|---|---|---:|---|---|
| 发布失败保持 pending、恢复重发、已入队但更新状态失败、重复消息 claim、并发重复领取、终态不重领（`test_job_dispatch.py`） | 集成（fake queue + 临时 JSON repository） | 否 | PASS | 全套 pytest；与本地文件 repository 交互，但 Redis 是 Fake。 |
| Redis/Postgres 实际发布与数据库持久化故障恢复 | 集成 | 否 | BLOCKED | 正常发布与消费已验证；Redis 发布失败、数据库写入失败、重复发布在 PostgreSQL repository 上尚未实际注入故障。 |
| Postgres repository + Redis Worker DEG 顺序链路 | 服务级 E2E | 是 | PASS | 本机 `omicsprism` / `omics_app`、Redis queue `omicsprism:e2e:pg:20261022`；Job `bf756f6c-3863-4fdc-8db7-4bb18db49c12` 创建 201、Worker succeeded、结果下载 200。 |
| Postgres repository + Redis Worker DEM/GMA 顺序链路 | 服务级 E2E | 是 | PASS | Redis queue `omicsprism:e2e:pg2:20261022`；DEM Job `66ef7696-d700-42c5-b674-ceb89ba2ad3d`、GMA Job `d91e7af6-1662-4372-85f1-fb458cbfae9c` 均 succeeded，DEM 表和 GMA PCA figure-data 可读取。 |
| 普通 API 创建 Job 到 Redis Worker 完成的 DEG 顺序链路 | 服务级 E2E | 是 | PASS（真实 Redis + 本地 JSON） | 隔离端口 `18087`、队列 `omicsprism:e2e:seq:20261022`；Job `2dcfce4a-906c-45fa-8839-55ac0158c557`，Preflight 200、创建 201、Worker 成功、结果下载 200。 |
| 普通 API 创建 Job 到 Redis Worker 完成的 DEM/GMA 顺序链路 | 服务级 E2E | 是 | PASS（真实 Redis + 本地 JSON） | 同一隔离环境；DEM Job `604a00c3-03b5-45b2-9bbd-8cbec67606c2`、GMA Job `7d2d3b1e-9576-4811-8e71-19ea83c56266` 均 succeeded，结果文件可读。 |
| API/Worker 并发更新本地 JSON 时保持 dispatch_state | 回归测试 | 否 | FAIL（已补 xfail 复现） | 真实服务中 DEG 曾返回 503（`WinError 32`）；DEM/GMA 最终 `status=succeeded` 但 `dispatch_state=pending`。新增 `test_cross_process_json_store_preserves_published_state_after_worker_progress` 固定该竞态。 |

### 结果 / Copilot 查询

| 测试名称 | 类型 | 真实计算 | 结果 | 证据 |
|---|---|---:|---|---|
| CSV/JSON 结果查询、引用、行边界和无结果/非法 artifact | 单元/服务测试 | 否，Fake 文件与 Job | PASS | `test_agent_result_tools.py`、`test_query_result_json.py`；结果按 owner 限制，跨用户返回 404。 |
| 图表 JSON 的 NaN/Infinity 边界 | 单元 | 否，合成 payload | PASS | `test_figure_data_boundaries.py`；严格 JSON 写入/读取断言。 |
| Copilot 查询真实计算产物并核对基因/代谢物/统计值 | E2E | 否 | BLOCKED | 现有测试以 Fake artifact 测试 grounding/query。没有连接真实 Job artifact 到 Copilot 最终证据回答。 |
| Playwright 全套（6 tests） | Mock E2E | 否 | PASS（6 passed） | 补齐 Agent 初始化、Job progress/event 和 pending interrupt mock 后，桌面/移动项目均通过；仍属于 Mock E2E。 |

### 汇总验证

- 后端全套：**370 passed, 14 skipped**，44 warnings，40.77 秒。命令：`.venv\Scripts\python.exe -m pytest backend/tests -q`。
- 前端 Vitest：**21 passed, 0 failed**。
- Playwright：**6 passed, 0 failed**（桌面和移动各运行三条场景）。
- 前端生产构建：**PASS**，`npm run build` 成功（Vite 5.4.21，存在大 chunk warning，不影响构建）。
- `git diff --check`：通过；本轮新增测试文件，不修改科学代码或生产业务代码。

## 3. 发现的问题

### P1：本地 JSON 存储与 Redis Worker 并发写入会丢失 dispatch 状态

- 复现：将 API 与 Worker 都配置为 `OMICS_PRISM_STORAGE_BACKEND=json`、`OMICS_PRISM_EXECUTOR=redis`，分别使用本地 JSON repository；提交 DEM/GMA Job，观察 Worker 成功后 `job.json` 的 `dispatch_state`，或重复提交 DEG 触发 API 返回 503。
- 根因：`JobStorageService` 锁仅在进程内有效；API 的 `mark_published` 与 Worker 的 `claim/update` 可能基于不同的陈旧 `JobRecord` 覆盖写入。Windows 文件替换还会产生 `WinError 32`。
- 影响：API 可能在消息已入 Redis 后返回 503；成功 Job 可能保持 `dispatch_state=pending`，导致错误的补偿发布/可观测性。
- 最小建议：本地 JSON + Redis 仅作为单进程开发模式；生产/跨进程 Redis 执行应使用 PostgreSQL Job repository。若必须支持 JSON 跨进程，需引入跨进程锁或基于最新记录的字段级原子更新，并保留并发回归测试。

### P1：端到端验收仍缺少真实 Postgres 存储和结果查询闭环

- 复现：启动平台所需的隔离 Postgres/Redis/文件存储及 Worker；使用普通表单或 Copilot 提交任一分析，等待真实 Worker 完成，再从结果 API 和 Copilot 读取该 Job artifact。
- 根因：本轮已验证真实 Redis 和本机 PostgreSQL 正常 Job 链路，但尚未完成故障注入以及 Playwright 到真实 API/Copilot 的闭环。Playwright 仍 mock Agent API。
- 影响：不能证明真实消息发布、跨进程消费、数据库状态持久化及 Copilot 对真实产物的权限/证据链。
- 最小建议：使用已确认的本机 `omicsprism` 数据库和 `omics_app` 账号，设置 `OMICS_PRISM_STORAGE_BACKEND=postgres` 与 runtime DSN，使用专用 Redis queue prefix，再增加服务级提交到结果查询测试；不得写入生产数据。

### P2：Vitest 的 Agent API 测试和实现不一致（已修复）

- 修复：已在 `frontend/src/copilot/agentApi.ts` 增加 `saveFeedback`，对应后端现有 PUT 路由；Vitest 21/21 通过。

### P2：Copilot Playwright mock 未覆盖页面初始化请求（已修复）

- 修复：已补齐 deterministic route mock，Playwright 6/6 通过；这些仍不是运行真实模型的证据。

### P3：Windows 默认 pytest 临时目录不可写

- 复现：未设置 `TEMP/TMP` 时直接运行指定后端测试，`tmp_path` 报 `PermissionError`；改指向仓库 `.test-tmp` 后通过。
- 根因：当前执行账号对系统 pytest 临时根目录没有创建权限。
- 影响：默认命令在此机器上不能可靠启动测试，但不是产品运行缺陷。
- 最小建议：本机/CI 将 `TEMP/TMP` 显式配置到工作区可写临时目录；无需生产代码改动。

## 4. 发布结论

- **已验证可用：** DEG、DEM、GMA 的真实 core pipeline 均通过 Platform Worker 执行函数完成；三者 Preflight 都通过。并且在真实 Redis + 本地文件存储环境中，普通 API 提交的 DEG、DEM、GMA 均成功被 Worker 消费并生成结果。DEG/DEM 统计表字段、GMA 网络结果表 schema、有限数值及输出文件可读取；产生的 JSON 文件可用严格解析器读取。GMA 真实输入按共同样本 ID 对齐，执行参数 `trans_log2=False`、`metab_log2=False` 被用于预处理。
- **仅 Mock/局部集成验证：** 表单请求字段一致、Copilot 确认 UI、Agent 参数契约、Job 发布补偿/去重、结果查询 grounding 和用户隔离。真实 Redis 与 PostgreSQL 正常 Job 链路已验证，但故障注入和 Copilot 真实 artifact 查询仍未完成。
- **尚未测试：** 浏览器普通表单提交到真实 Worker 完成；Agent 多轮参数修改再跑真实引擎；真实运行 artifact 被 Copilot 查询并核对数值；Postgres repository 下的失败恢复/重复发布场景。Postgres DEG 正常链路已验证；本地 JSON 跨进程状态竞态仍未修复。
- **P0 结论：** 本轮没有发现已复现的 P0 数据丢失或科学正确性缺陷；由于真实服务链路、真实 Copilot 证据链和统计参考输出比较未完成，不能据此宣布不存在此类缺陷。
- **发布判定：** **阻止本轮全链路验收通过，状态 BLOCKED。** 当前三条科学引擎执行回归可作为局部通过证据；全链路验收需先解决前端红灯并在隔离服务环境补跑普通表单/Agent 到结果查询闭环。
