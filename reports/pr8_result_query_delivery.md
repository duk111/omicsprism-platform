# PR 8：Agent 结果查询语义交付

日期：2026-10-26（Asia/Shanghai）。分支：`fix/agent-result-query-semantics`，起点 `fe7d3f5`，开始时工作区干净。当前改动保留在工作区，未提交、未推送。

## 结果

DEG/DEM 实体查询按真实表头定位 `gene_id`/`metabolite_id`；GMA 网络表支持明确的 Gene/Metabolite 查询并拒绝未限定类型的歧义解析。CSV 数值比较和排序使用有限 Decimal，零值有效，同值稳定，缺失/NaN/Inf 置后。过滤、排序、limit 不改写原始行号或源字段。真实 SHA-256 内容不一致、字段错误或超限单行都不会生成有效证据。

完整真实产物字段对照、调用链、既有优化核查、协议兼容说明见 `pr8_result_query_audit.md`。没有改动科学核心、参数默认值、LangGraph 结构、Agent 角色、数据库或前端页面；没有增加依赖。

## 修改文件清单

| 文件 | 变更 |
|---|---|
| `backend/app/agent/tools.py` | 表头映射、有限数值比较/稳定排序、字段和歧义校验、Checksum 核验、响应限制；大 CSV Schema 只读取头部 |
| `backend/app/agent/readonly_tools.py` | 现有 scalar filters/resolve_entity 的语义描述；没有增加参数或响应字段 |
| `backend/app/agent/model.py` | 结果角色使用实际 Schema、明确 GMA 实体类型和比较键的提示词 |
| `backend/app/agent/router.py` | 修复中文指定代谢物的已有结果查询误入分析路径 |
| `backend/tests/test_agent_result_query_semantics.py` | 61 个新增参数化回归，含 6 个图级三问题/双入口场景 |
| `backend/tests/test_agent_router.py` | 中文结果查询路由回归 |
| `backend/tests/test_agent_artifact_e2e.py` | 既有 invoke helper 支持真实用户问题文本 |
| `backend/tests/test_agent_readonly_tools.py` | 旧查询 fixture 使用与内容一致的真实摘要 |
| `backend/app/agent/fixtures/result_query/deg.all.csv` | 6 条真实 Worker DEG 输出摘录 |
| `backend/app/agent/fixtures/result_query/dem.all.csv` | 6 条真实 Worker DEM 输出摘录 |
| `backend/app/agent/fixtures/result_query/T02_High_Confidence_Network.csv` | 8 条真实 Worker GMA 输出摘录 |
| `backend/app/agent/fixtures/result_query/manifest.json` | 产物来源、核心 commit、源摘要和摘录摘要 |
| `reports/pr8_result_query_audit.md` | 阶段 A 审计和兼容性记录 |
| `reports/pr8_result_query_delivery.md` | 本交付记录 |

`grounding.py`、`verifier.py`、`context.py`、公开 ToolResult/Citation、前端证据组件均沿用已有实现。

## 修复前后对比

以下使用相同输入，分别在基线源码和当前源码执行（数据为显式标注的合成边界样本，真实核心摘录另行回归）。完整响应保存在本地 `reports/pr8-comparison.json`。

| 请求 | 修复前 | 修复后 |
|---|---|---|
| DEG `resolve_entity=GeneA` | 成功但 0 行，错误注入 `Gene` | 真实 `gene_id=GeneA`，原记录 2 |
| DEM `resolve_entity=MetA` | 成功但 0 行，错误注入 `Gene` | 真实 `metabolite_id=MetA`，原记录 2 |
| padj 升序 limit=5，混有 NaN/Inf/空值 | 原记录 `[1,9,7,4,8]`，包含非有限值 | 原记录 `[7,4,8,6,5]`，值 `[0,0.001,1e-3,0.02,0.1]` |
| `log2FoldChange__gt=1` 后 padj 升序 | 被当作不存在列，静默 0 行 | 原记录 `[2,1]`，筛选和排序数值语义正确 |
| 不存在字段等于空字符串 | 错误匹配原记录 1 | `ok=false, error_code=field_not_found` |
| 不存在实体 | 空成功结果，无状态区分 | `ok=true, error_code=no_match`，保留原 Artifact/Checksum，空引用不支持实体结论 |
| GMA 双实体表仅给 `resolve_entity` | 任意默认选 Gene | `ambiguous_entity_field`；改用明确 `filters={Gene:...}` 或 `{Metabolite:...}` |

证据回归：原记录 2 排到结果第 1 项后 Citation 仍为 `[2]`；错误 `[1]`、错误实体、错误字段数值和错误 Checksum 均被 PR 7 验证链拒绝。

## 测试结果

| 测试组 | 结果 | 本地日志 |
|---|---|---|
| 最初 red 阶段 | 35 failed，确认实体/比较/排序/字段/摘要问题；4 个真实 fixture 断言最初错误要求 sha256 前缀，已按存储真实裸摘要修正 | `reports/pr8-red.log` |
| Agent 问题与超限边界 red 阶段 | 3 failed, 44 passed；定位中文 DEM 路由、超限无证据误报 | `reports/pr8-agent-red.log` |
| 大表 Schema red 阶段 | 1 failed, 53 passed；定位大文件无法描述列名 | `reports/pr8-boundary-red.log` |
| claim 上限 red 阶段 | 1 failed, 60 passed；定位单行超过既有 GroundedClaim 字符上限 | `reports/pr8-claim-red.log` |
| 最终定向 | **163 passed**，含新增语义 61、结果工具 5、JSON 10、落盘端到端 11、PR 7 evidence 35、Grounding 6、router 14、readonly 5、capability 3、MCP 9、grounded eval 4 | `reports/pr8-targeted.log` |
| 真实核心和既有参数/发布回归 | **51 passed, 1 xfailed**；DEG/DEM/GMA 三个真实计算 pipeline + 实际输出进入 Copilot 均通过 | `reports/pr8-core.log` |
| 最终后端全套 | **486 passed, 14 skipped, 1 xfailed, 1 failed**，502 个测试；唯一失败已在未修改 PR 7 基线复现 | `reports/pr8-full-final.log` |
| 基线独立复现 | 同一 `test_agent_eval_v2.py:152` 失败，capability pass@1=4/6 | `reports/pr8-baseline.log` |
| 既有 Agent domain eval | **passed=true**；参数 3/3、歧义 4/4、权限能力隔离 3/3；grounded fixture 正确识别故意错误实体样本 | `reports/pr8-domain-eval.log` |
| Agent Eval v2 CI | release gate 通过；42/42 质量场景通过，引用/数值一致性 1.0、unsupported claim 0；独立 capability 场景 4/6 为已有问题 | `reports/pr8-eval.log`，`.test-tmp/pr8-eval-v2.json` |
| 前端现有测试 | **21 passed / 7 files**，含 MessageBlocks 引用组件 | `reports/pr8-frontend.log` |
| Git | `git diff --check` 通过；科学核心仓库仍为干净 master | 当前工作区 |

三个指定问题均使用 ScriptedModel + 实际图 + capability registry + 文件存储执行，不依赖真实模型服务。每个问题同时覆盖 tool_call/query_result 入口；回答逐字段对应实际返回行，GeneZ 错误草稿不能替换真实实体。真实 DEG/DEM/GMA fixture 的原始实体 ID 和细胞值未被替换；指定 GeneA/MetA 问题使用完整真实列结构的合成边界样本，测试中有明确说明。没有宣称完成真实远端模型 E2E。

## 可复现命令

工作目录 `D:\JetBrains\PythonProjects\omicsprism-platform`，使用已存在 `.venv`。PowerShell 的 Tee-Object 日志中可能出现 GBK 控制台乱码，但测试退出码、JUnit 和 UTF-8 源码均可核验。日志由仓库既有 `*.log` 规则忽略，本地保留，无需修改忽略规则。

```powershell
# 最终定向（共 163 个）
.venv/Scripts/python.exe -m pytest backend/tests/test_agent_result_query_semantics.py backend/tests/test_agent_result_tools.py backend/tests/test_query_result_json.py backend/tests/test_agent_artifact_e2e.py backend/tests/test_agent_evidence_reliability.py backend/tests/test_agent_grounding.py backend/tests/test_agent_router.py backend/tests/test_agent_readonly_tools.py backend/tests/test_agent_capabilities.py backend/tests/test_agent_mcp_adapter.py backend/tests/test_agent_eval_grounded_qa.py -q --tb=short --basetemp=.test-tmp/pr8-targeted-final-3 --junitxml=.test-tmp/pr8-targeted-junit.xml 2>&1 | Tee-Object reports/pr8-targeted.log

# 真实核心、参数、既有发布恢复
.venv/Scripts/python.exe -m pytest backend/tests/test_scientific_engine_integration.py backend/tests/test_job_execution_params.py backend/tests/test_confirmation_modify.py backend/tests/test_param_resolver.py backend/tests/test_job_dispatch.py -q --tb=short --basetemp=.test-tmp/pr8-core 2>&1 | Tee-Object reports/pr8-core.log

# 最终完整后端（退出码 1，原因见已证实的基线失败）
.venv/Scripts/python.exe -m pytest backend/tests -q --tb=short --basetemp=.test-tmp/pr8-full-final --junitxml=.test-tmp/pr8-full-final-junit.xml 2>&1 | Tee-Object reports/pr8-full-final.log

# 现有评估
.venv/Scripts/python.exe scripts/run_agent_eval.py --json 2>&1 | Tee-Object reports/pr8-domain-eval.log
.venv/Scripts/python.exe scripts/run_agent_eval_v2.py --json --output .test-tmp/pr8-eval-v2.json 2>&1 | Tee-Object reports/pr8-eval.log

# frontend/ 下
npm test -- --reporter=dot 2>&1 | Tee-Object ../reports/pr8-frontend.log
```

基线通过 `git archive --format=zip fe7d3f5 backend scripts` 解压到独立临时目录，在该目录使用相同虚拟环境执行 `-m pytest backend/tests/test_agent_eval_v2.py::test_evaluator_self_tests_keep_malformed_evidence_out_of_agent_quality_metrics -q --tb=short`；没有切换或覆盖用户当前分支。

## 剩余问题与性能

1. CSV 实际查询仍全量读入：一次本地 tracemalloc 测量，10,000 行/177,808 bytes 约 0.183 秒、6.24 MiB；100,000 行/1,977,790 bytes 约 1.843 秒、62.93 MiB。数据为三列合成 CSV，输入字符串在 tracing 前构造，因此这只是额外分配估计，非生产容量结论。后续可基于现有 `open_artifact` 流式 enumerate 过滤和计数，使用稳定 top-k/有限堆，整文件 SHA-256 另用流式摘要；保持数据记录号语义。未引入查询引擎/数据库。详细记录 `reports/pr8-performance.json`。
2. 既有 Eval v2 的 metadata 和 contrasts capability 场景在执行工具后进入 clarification interrupt，和 fixture 的 completed 期望不符，故 4/6；该问题已在 `fe7d3f5` 复现，未修改预期或绕过断言。日志中的非法 get_jobs_status 拒绝只是已有负向测试输出，不是这两项差异的原因。
3. 现有 LocalJsonJobRepository 跨进程 dispatch_state 竞态仍为 1 xfail；14 skipped 包含未配置专用 PostgreSQL 权限环境的 2 项和已被当前图流程替代的 12 项。保留其状态，没有声称通过。
4. 本轮 CSV 扩展不覆盖 JSON 图表的数值比较语义；JSON 10 项既有回归通过。GMA T99、表达矩阵、OPLS-DA scores 仍不扩展白名单；T06 等汇总表的复合/Top 字段需要使用真实列名显式过滤。
5. 单条原始证据超过既有 1000 字符 claim 限制明确失败；没有通过裁剪单元格、重写结果表或放宽 PR 7 验证器来掩盖问题。

## Git Diff 摘要

生产修改集中在 4 个 Agent 文件；测试 4 个（含新增语义测试），真实 fixture 4 个，报告 2 个，共 14 个文件。已有跟踪文件 `git diff --stat` 为 7 files changed, 213 insertions, 20 deletions；新增文件单独列于上述清单。未改前端/科学核心/依赖/数据库，未修改 Grounding 或 Verifier。
