# PR 8 结果查询审计

日期：2026-10-26（Asia/Shanghai）。基线分支：`master`，PR 7 提交：`fe7d3f5`。科学核心提交：`a17ee2291789cdc0ee2db62baa0713106d521a12`。

## 已有优化实际状态

- `dc5c53e` 的合法零值/False 和科学默认值映射，以及 `a4c76d7` 的执行参数、Preflight 和真实核心集成测试均在当前代码中。
- `de342ed` 的 GMA Agent 参数修改和多轮状态保留实现仍在 `param_resolver.py` 及已有图回归中。本轮未更改参数默认值。
- `d7a2dbb` 的任务发布、重发恢复、重复领取保护仍在 Job store/执行代码中；已有跨进程 JSON 发布状态竞态仍标为 xfail，不能宣称基础可靠性完全无缺陷。
- `fe7d3f5` 的 GroundedAnswerPipeline、实体/字段/数值绑定、错误草稿降级、禁止无证据结果回答等已在源码和测试中核实。此次没有重写这些实现。

## 真实 Artifact 字段

| 分析/Artifact | 当前核心实际字段 | 实体字段 |
|---|---|---|
| DEG `*.all.csv` / `*.sig.csv` | `gene_id, baseMean, log2FoldChange, lfcSE, stat, pvalue, padj, comparison, volcano_status` | `gene_id` |
| DEM `*.all.csv` / `*.sig.csv` | `metabolite_id, tested_mean, reference_mean, fold_change, log2FoldChange, t_stat, pvalue, vip, comparison, tested_level, reference_level, n_tested, n_reference, padj_bh, dem_status` | `metabolite_id` |
| GMA `T01_Metabolite_Association_Summary.csv` | `Metabolite, CandidateGenes, PCCSelectedGenes, SpearmanSelectedGenes, MISelectedGenes, TargetK, MeanScreenSupportCount, MeanModelSupportCount, DualModelEdges, MultiScreenEdges, TotalAssociationEdges, HighConfidenceEdges, TopGene, TopEdgeWeight` | `Metabolite` / `TopGene`（字段语义不同） |
| GMA `T02_High_Confidence_Network.csv` | `Source, Target, Interaction, Gene, Metabolite, PearsonR, PearsonFDR, SpearmanRho, SpearmanFDR, MIScore, ScreenSupportCount, ModelSupportCount, RRARank, EdgeWeight, Sign, EdgeTier` | `Gene`、`Metabolite` |
| GMA `T03_Key_Gene_Summary.csv` | `Gene, AssociatedMetaboliteCount, AssociatedMetabolites, MeanRRARank, BestRRARank, MeanEdgeWeight, BestEdgeWeight, HighConfidenceMetaboliteCount, HighConfidenceMetabolites` | `Gene` |
| GMA `T04_Gene_Module_Assignment.csv` | `Gene, Module, ModuleColorHex, ModuleSize, kME, IntramodularDegree, IsGrey, BestEdgeWeight, AssociatedMetaboliteCount, HighConfidenceMetaboliteCount` | `Gene` |
| GMA `T05_Module_Metabolite_Association.csv` | `Module, Metabolite, SpearmanRho, FDR` | `Metabolite` |
| GMA `T06_Module_Summary.csv` | `Module, ModuleColorHex, ModuleSize, MeanKME, MeanIntramodularDegree, TopHubGene, TopHubKME, MetaboliteAssociationCount, TopMetabolite, TopMetaboliteRho` | `TopHubGene` / `TopMetabolite` |

其他差异分析产物：

| Artifact | 当前核心实际字段 | 查询边界 |
|---|---|---|
| DEG `differential_gene_counts.csv` | `comparison, up_count, down_count, significant_count, non_significant_count, total_genes` | 比较级计数，无 gene_id，不能用于基因 padj 排名 |
| DEM `differential_metabolite_counts.csv` | `comparison, up_count, down_count, significant_count, non_significant_count, total_metabolites` | 比较级计数，无 metabolite_id |
| DEG `union_significant_genes.csv` | `gene_id, n_significant_contrasts, best_padj, max_abs_log2FoldChange` | 跨比较并集，不能支持单个比较的上/下调结论 |
| DEM `union_significant_metabolites.csv` | `metabolite_id, n_significant_contrasts, best_padj, best_pvalue, max_vip, max_abs_log2FoldChange` | 跨比较并集，保留字段的聚合语义 |
| DEG `union_significant_genes.vst.csv` / DEM `union_significant_metabolites.matrix.csv` | 实体列加动态样本列 | 当前结果白名单不放开这些矩阵 |
| DEM `*.oplsda_scores.csv` | `sample_id, class_label, tp1`，存在正交成分时还有 `to1` | 当前结果白名单不放开该文件 |

GMA `T99_Metabolite_Gene_Scoring_Audit.csv` 为显式开启的审计输出，当前结果查询白名单不开放。`T05` 的内部计算表有 `PValue`，导出表由 `_module_metabolite_export_columns()` 明确排除该列。表不存在或某个可选字段未被导出时，以读取的 CSV 表头为准。`TopGene`/`TopHubGene`/`TopMetabolite` 仅支持显式字段等值过滤，不纳入未限定实体的自动解析。

取证来源：科学核心 `deg/pipeline.py` 的 `_standardize_results_df()`、`run_deg_pipeline()` 和 union 导出；`dem/pipeline.py` 的单比较结果构造和 union/score 导出；`core.py` 的 `_analysis_network_export_columns()`、`_gene_module_export_columns()`、`_module_metabolite_export_columns()`、各 summary 构造和 `save_results()`；另逐一比对留存 Worker CSV 文件头。

真实 Worker 输出摘录保存在 `backend/app/agent/fixtures/result_query/`，manifest 记录源文件、核心 commit 和 SHA-256。

性能记录见 `reports/pr8-performance.json`：当前实现对约 1.98 MB/100,000 行 CSV 的一次查询约 1.84 秒、峰值 tracemalloc 约 62.9 MiB；这确认了全量读取风险，但未达到本轮必须重构查询引擎的正确性阻断条件。

## 已确认缺陷

1. CSV `resolve_entity` 固定注入 `Gene`，DEG/DEM 实体查询因此静默为空。
2. 字段不存在时原查询把 `row.get()` 当空字符串；空表或空匹配无法区分字段错误、实体不存在和成功无匹配。
3. 过滤没有数值比较；排序对 `NaN`/无穷值和缺失值没有科学语义保证。
4. `row_count` 在截断前统计是正确的，但排序后必须继续返回原始文件 `_row_id`，否则 PR 7 的引用会错位。
5. 查询仍一次性读取 CSV；本轮保留已有响应大小/行数上限，只在 `describe_artifacts` 使用流式表头读取。大文件查询引擎拆分留给性能 PR。

## Grounding/Verifier 兼容性

查询继续返回原始 Artifact path、注册 Checksum 和原始 CSV 行号；排序只改变返回顺序，不重编号。`query_result_evidence`、`GroundedAnswerPipeline`、`AnswerVerifier`、前端 `AgentEvidenceBlock` 协议未改变。Checksum 与实际内容不一致时查询失败，不生成可引用证据。

Checksum 校验兼容存储中的裸 SHA-256 和 `sha256:` 前缀两种格式，引用仍沿用注册值原貌；已有非摘要的 opaque token 保持历史兼容，只能做身份绑定，不能当作内容完整性校验。真实存储产物均由文件摘要生成。测试覆盖 CRLF、BOM 和 CSV 引号内换行；`_row_id` 是从 1 开始、不含表头的 CSV **数据记录序号**，不是文本编辑器物理行号。重复表头及伪造 `_row_id` 表头拒绝作为 CSV 证据。

调用链已检查：`context.py` 提供受当前用户约束的 Job/artifact 索引；结果角色先使用 `describe_artifacts`、再经 capability registry 校验参数执行 `query_artifact`，或通过 `ResultQuerySpec`/`result_querier_from_runtime` 执行 `query_result`。两者共用 `_query_artifact`。所有筛选/排序先保留记录号，再交给 GroundedAnswerPipeline 和 AnswerVerifier。前端 `MessageBlocks.tsx` 直接展示原 `artifact/checksum/row_ids`，没有字段变更或页面修改。

## 最小修复

- 根据 `AnalysisType` 和实际表头选择实体字段：DEG `gene_id`，DEM `metabolite_id`；GMA 双实体表要求明确 `Gene` 或 `Metabolite`。
- 只增加明确映射：DEG/DEM/GMA 的实体别名，以及 DEM `padj` 到真实 `padj_bh` 的查询别名。
- 保留等值过滤；增加受限的 `field__lt/lte/gt/gte/eq/ne` 和 `field__abs_gt/abs_gte/abs_lt/abs_lte` 数值比较。
- 使用 Decimal 解析有限数值，稳定排序；缺失、NaN、Inf 始终置后；排序后再限制数量。
- 返回 `no_match`、`field_not_found`、`entity_field_not_found`、`ambiguous_entity_field`、`invalid_filter`、`invalid_sort`、`checksum_mismatch`、`result_exceeds_response_limit` 等可区分状态。

兼容方式：不添加公开工具参数或响应字段，`filters` 仍是至多 8 个 scalar value 的字典，原有字符串等值调用不变；数值 scalar 等值按数值比较，实体 ID 始终精确文本匹配。新比较键属于受限的向后兼容语义扩展，已写入 QueryArtifactRequest 描述和结果角色提示词。没有 OR、正则、表达式执行、SQL、字段间运算或函数语言。JSON 图表查询仍使用原有精确等值协议，本轮比较扩展只针对 CSV。

无匹配是 `ok=true, error_code=no_match, rows=[], row_count=0`；字段错误和实体歧义是 `ok=false`，不会进入 Grounding。工具默认最多 50 行、32 KiB；旧 `query_result` 模型决策仍遵循已有 12 行 limit 约束。先过滤，再稳定排序，再 limit 和字节截断，`row_count` 为筛选后的总匹配数。单行无法满足既有 1000 字符 GroundedClaim/响应字节限制时明确失败，不截短原始字段或伪造引用。

安全链保持：`get_for_user(job_id, user_id)` → 分析类型白名单 → 已登记 Artifact 精确匹配 → 存储 `_workspace_path()` 的 resolve/is_relative_to 校验。字段名只用于内存中的表头映射，不参与文件路径拼接。跨用户查询继续 404。

## 测试覆盖

新增 `test_agent_result_query_semantics.py` 覆盖 DEG/DEM/GMA 实体、GMA 双实体歧义、真实核心 fixture、比较/绝对值筛选、数值排序、NaN/Inf/空值/零值、限制顺序、原始行号、Checksum、权限、错误字段、超大行和 Agent 图级三类问题。另补充中文结果查询路由测试。
