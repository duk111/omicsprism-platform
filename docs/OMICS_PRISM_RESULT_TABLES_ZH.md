# OmicsPrism 三个分析模块结果表说明

> 文档版本：1.0
> 对应项目版本：0.5.0
> 更新日期：2026-07-22
> 适用模块：DEG、DEM、GMA

## 1. 文档目的

本文介绍 OmicsPrism 三个分析模块生成的结果表，包括：

- 每张表解决什么问题。
- 文件名和生成条件。
- 每个主要字段的含义。
- 显著性和排序规则。
- 哪些结果可继续作为 GMA 输入。

本文只说明结构化结果表，不详细解释 SVG/PNG 图和 HTML 报告。

## 2. 模块与文件概览

| 模块 | 主要结果表 |
| --- | --- |
| DEG | `{contrast}.all.csv`、`{contrast}.sig.csv`、`differential_gene_counts.csv`、`union_significant_genes.csv`、`union_significant_genes.vst.csv` |
| DEM | `{contrast}.all.csv`、`{contrast}.sig.csv`、`{contrast}.oplsda_scores.csv`、`differential_metabolite_counts.csv`、`union_significant_metabolites.csv`、`union_significant_metabolites.matrix.csv` |
| GMA | `T01_Metabolite_Association_Summary.csv` 至 `T06_Module_Summary.csv`，以及可选的 `T99_Metabolite_Gene_Scoring_Audit.csv` |

`{contrast}` 是由实验设计生成的对比名称。使用 `same_fields=line,timepoint`、`compare_field=treatment`、tested level 为 `salt`、reference level 为 `control` 时，对比名称中通常会包含 line、timepoint 和 salt-vs-control 信息，具体格式以实际输出文件名为准。

## 3. DEG 结果表

DEG 使用 PyDESeq2 对 RNA-seq raw count 进行差异表达分析。每一个有效 contrast 都会生成一张全量结果表和一张显著结果表。

### 3.1 `{contrast}.all.csv`

用途：保存某个对比中所有通过基础过滤并进入 DESeq2 统计分析的基因，无论是否显著。

| 字段 | 含义 |
| --- | --- |
| `gene_id` | 基因 ID，来自输入 count 矩阵的行名 |
| `baseMean` | DESeq2 标准化 count 在所有参与该模型样本中的平均值 |
| `log2FoldChange` | tested group 相对于 reference group 的 log2 倍数变化 |
| `lfcSE` | `log2FoldChange` 的标准误 |
| `stat` | DESeq2 Wald 检验统计量 |
| `pvalue` | 原始 P 值 |
| `padj` | 多重检验校正后的 P 值 |
| `comparison` | 当前对比名称 |
| `volcano_status` | 按当前阈值标记为 `Up`、`Down` 或 `Non-significant` |

方向解释：

- `log2FoldChange > 0`：tested group 中表达更高。
- `log2FoldChange < 0`：tested group 中表达更低。
- `log2FoldChange = 1`：tested/reference 约为 2 倍。
- `log2FoldChange = -1`：tested/reference 约为 0.5 倍。

默认分类规则：

```text
Up:
  padj < 0.05
  log2FoldChange >= 1

Down:
  padj < 0.05
  log2FoldChange <= -1

Non-significant:
  不同时满足以上显著性和倍数变化条件
```

实际阈值由任务参数 `padj_cutoff` 和 `log2fc_cutoff` 决定。

注意：`padj` 可能为空。常见原因是基因信息量不足、P 值不可用，或独立过滤后无法计算校正值。空值不能按显著基因处理。

### 3.2 `{contrast}.sig.csv`

用途：仅保留当前 contrast 中达到显著阈值的 DEG。

字段与全量表的统计字段一致：

```text
gene_id
baseMean
log2FoldChange
lfcSE
stat
pvalue
padj
comparison
```

筛选规则为：

```text
padj < padj_cutoff
且
abs(log2FoldChange) >= log2fc_cutoff
```

结果默认按 `padj` 从小到大、`gene_id` 从小到大排序。

该表不包含 `volcano_status`，方向可根据 `log2FoldChange` 正负判断。

### 3.3 `differential_gene_counts.csv`

用途：按 contrast 汇总上调、下调和非显著基因数量，适合快速比较不同实验条件下 DEG 数量。

| 字段 | 含义 |
| --- | --- |
| `comparison` | 对比名称 |
| `up_count` | `volcano_status=Up` 的基因数量 |
| `down_count` | `volcano_status=Down` 的基因数量 |
| `significant_count` | `up_count + down_count` |
| `non_significant_count` | 非显著基因数量 |
| `total_genes` | 当前 contrast 全量结果中的基因总数 |

校验关系：

```text
significant_count = up_count + down_count
total_genes = significant_count + non_significant_count
```

### 3.4 `union_significant_genes.csv`

用途：合并所有 contrast 的显著基因并去重，一行代表一个至少在一个 contrast 中显著的基因。

| 字段 | 含义 |
| --- | --- |
| `gene_id` | 基因 ID |
| `n_significant_contrasts` | 该基因显著出现过的不同 contrast 数量 |
| `best_padj` | 所有显著 contrast 中最小的 `padj` |
| `max_abs_log2FoldChange` | 所有显著 contrast 中最大的绝对 `log2FoldChange` |

该表默认按 `best_padj` 从小到大排序。

建议用途：

- 获取跨条件 DEG 候选集合。
- 根据 `n_significant_contrasts` 查找在多个条件下重复出现的基因。
- 根据 `best_padj` 和 `max_abs_log2FoldChange` 做二次优先级排序。

这张表不保留每个 contrast 的方向。需要判断具体条件下上调或下调时，应回查对应的 `{contrast}.all.csv` 或 `{contrast}.sig.csv`。

### 3.5 `union_significant_genes.vst.csv`

用途：导出所有显著基因并集的 VST 表达矩阵，可直接作为 GMA 的转录组输入。

格式：

```text
gene_id,S1,S2,S3,...
GeneA,7.12,7.34,6.98,...
GeneB,4.63,4.81,4.27,...
```

| 字段 | 含义 |
| --- | --- |
| `gene_id` | 基因 ID |
| 后续各列 | 对应样本的 VST 表达值 |

矩阵方向为 genes × samples。

重要说明：该表已经完成 VST 变换。将其作为 GMA 转录组输入时，不应再次执行 `log2(x+1)`，本地 CLI 应使用 `--no-trans-log2`；Web 平台中应关闭 transcriptome Log2 选项。

## 4. DEM 结果表

DEM 综合使用代谢物丰度差异、Welch t 检验、BH 校正和 OPLS-DA VIP 进行筛选。

### 4.1 `{contrast}.all.csv`

用途：保存某个 contrast 中所有进入 DEM 分析的代谢物。

| 字段 | 含义 |
| --- | --- |
| `metabolite_id` | 代谢物 ID，来自输入矩阵行名 |
| `tested_mean` | tested group 中经过填补及可选 median normalization 后的平均丰度 |
| `reference_mean` | reference group 中对应的平均丰度 |
| `fold_change` | `(tested_mean + pseudocount) / (reference_mean + pseudocount)` |
| `log2FoldChange` | `log2(fold_change)` |
| `t_stat` | Welch t 检验统计量 |
| `pvalue` | Welch t 检验原始 P 值 |
| `vip` | OPLS-DA Variable Importance in Projection 分数 |
| `comparison` | 当前对比名称 |
| `tested_level` | tested level 原始名称 |
| `reference_level` | reference level 原始名称 |
| `n_tested` | tested group 中原始非缺失观测数量 |
| `n_reference` | reference group 中原始非缺失观测数量 |
| `padj_bh` | Benjamini-Hochberg 校正后的 P 值 |
| `dem_status` | `Up`、`Down` 或 `Non-significant` |

方向解释与 DEG 一致：正的 `log2FoldChange` 表示 tested group 丰度更高，负值表示 tested group 丰度更低。

默认显著分类要求同时满足：

```text
vip >= 1.0
padj_bh <= 0.05
abs(log2FoldChange) >= 1.0
```

满足条件且 `log2FoldChange > 0` 标记为 `Up`；小于 0 标记为 `Down`。

注意：

- `tested_mean` 和 `reference_mean` 是用于 fold change 的归一化尺度，不一定等同于原始峰面积。
- Welch t 检验使用经过可选 Log2 处理的数据。
- VIP 使用 Log2 后再进行 Pareto scaling 的数据计算。
- `n_tested` 和 `n_reference` 是填补前实际观测数，可用于识别缺失值较多的代谢物。

### 4.2 `{contrast}.sig.csv`

用途：仅保留 `dem_status` 为 `Up` 或 `Down` 的代谢物。

字段与 `{contrast}.all.csv` 完全一致。结果默认按：

1. `pvalue` 从小到大。
2. `vip` 从大到小。
3. `metabolite_id` 从小到大。

该表已经同时应用 VIP、校正 P 值和 fold change 阈值，不应只根据 VIP 再次定义显著性。

### 4.3 `{contrast}.oplsda_scores.csv`

用途：保存 OPLS-DA 模型中的样本得分，用于绘制和复核 OPLS-DA score plot。

| 字段 | 含义 |
| --- | --- |
| `sample_id` | 样本 ID |
| `class_label` | 当前 `compare_field` 对应的分组标签 |
| `tp1` | 第一预测成分得分，反映与组别区分相关的方向 |
| `to1` | 第一正交成分得分；仅在配置了正交成分且模型成功计算时存在 |

该表的行单位是样本，不是代谢物。`tp1` 或 `to1` 的绝对值不能直接解释为代谢物的重要性，代谢物重要性应查看 `vip`。

### 4.4 `differential_metabolite_counts.csv`

用途：按 contrast 汇总差异代谢物数量。

| 字段 | 含义 |
| --- | --- |
| `comparison` | 对比名称 |
| `up_count` | 上调差异代谢物数量 |
| `down_count` | 下调差异代谢物数量 |
| `significant_count` | 上调和下调数量之和 |
| `non_significant_count` | 非显著代谢物数量 |
| `total_metabolites` | 当前 contrast 的代谢物总数 |

### 4.5 `union_significant_metabolites.csv`

用途：合并所有 contrast 的显著代谢物并去重。

| 字段 | 含义 |
| --- | --- |
| `metabolite_id` | 代谢物 ID |
| `n_significant_contrasts` | 显著出现过的 contrast 数量 |
| `best_padj` | 所有显著 contrast 中最小的 `padj_bh` |
| `best_pvalue` | 所有显著 contrast 中最小的原始 P 值 |
| `max_vip` | 所有显著 contrast 中最大的 VIP |
| `max_abs_log2FoldChange` | 所有显著 contrast 中最大的绝对 log2 fold change |

结果默认按 `best_padj` 从小到大排序。

该表适合筛选跨条件代谢物候选，但同样不保留每个 contrast 的上调/下调方向，方向需要回查 contrast 表。

### 4.6 `union_significant_metabolites.matrix.csv`

用途：导出显著代谢物并集对应的样本丰度矩阵，可作为 GMA 代谢组输入。

格式：

```text
metabolite_id,S1,S2,S3,...
M001,120.5,98.2,135.7,...
M002,42.8,51.4,47.9,...
```

矩阵方向为 metabolites × samples。

重要说明：该文件保存的是与输入样本对齐后的原始代谢物矩阵子集，不是每个 contrast 内填补、归一化、Log2 或 Pareto scaling 后的矩阵。因此将其用于 GMA 时，是否开启 metabolome Log2 应根据原始输入数据尺度决定，而不能仅因为该文件来自 DEM 就默认关闭。

## 5. GMA 结果表

GMA 对每个代谢物执行三路候选筛选、ElasticNet/XGBoost 建模和 RRA 聚合，再构建高置信基因-代谢物网络和基因模块。

正式结果表使用 `T01` 至 `T06` 编号。某个分析阶段没有可用结果时，对应表可能不会生成。例如高置信网络为空时，模块分析表通常不会生成。

### 5.1 `T01_Metabolite_Association_Summary.csv`

用途：以代谢物为单位汇总候选基因、模型支持和关联网络规模。一行代表一个代谢物。

| 字段 | 含义 |
| --- | --- |
| `Metabolite` | 代谢物 ID |
| `CandidateGenes` | 三路筛选合并后的候选基因数量 |
| `PCCSelectedGenes` | Pearson 筛选选中的基因数量 |
| `SpearmanSelectedGenes` | Spearman 筛选选中的基因数量 |
| `MISelectedGenes` | Mutual Information 筛选选中的基因数量 |
| `TargetK` | 当前代谢物模型计划保留的目标基因数量 |
| `MeanScreenSupportCount` | 候选边平均获得的筛选方法支持数，范围通常为 0 至 3 |
| `MeanModelSupportCount` | 候选边平均获得的模型支持数，范围通常为 0 至 2 |
| `DualModelEdges` | 同时被 ElasticNet 和 XGBoost 选中的边数 |
| `MultiScreenEdges` | 至少被两种筛选方法支持的边数 |
| `TotalAssociationEdges` | 至少被一种机器学习模型选中的关联边数 |
| `HighConfidenceEdges` | 达到高置信规则的边数 |
| `TopGene` | 当前代谢物 `EdgeWeight` 最高的基因 |
| `TopEdgeWeight` | TopGene 对应的综合边权重 |

结果优先按 `HighConfidenceEdges`、`TotalAssociationEdges`、`CandidateGenes` 从大到小排序。

建议先用该表判断哪些代谢物具有较丰富且稳定的基因关联，再进入网络表查看具体基因。

### 5.2 `T02_High_Confidence_Network.csv`

用途：GMA 最核心的边表。一行代表一条高置信基因-代谢物关联边，可用于 Cytoscape、Gephi 或其他网络工具。

| 字段 | 含义 |
| --- | --- |
| `Source` | 网络源节点，当前等于基因 ID |
| `Target` | 网络目标节点，当前等于代谢物 ID |
| `Interaction` | 固定为 `association` |
| `Gene` | 基因 ID |
| `Metabolite` | 代谢物 ID |
| `PearsonR` | Pearson 相关系数 |
| `PearsonFDR` | Pearson P 值的 FDR |
| `SpearmanRho` | Spearman 秩相关系数 |
| `SpearmanFDR` | Spearman P 值的 FDR |
| `MIScore` | Mutual Information 分数，表示非线性依赖强度 |
| `ScreenSupportCount` | PCC、Spearman、MI 三种筛选中支持该边的方法数 |
| `ModelSupportCount` | ElasticNet、XGBoost 两种模型中支持该边的模型数 |
| `RRARank` | 在当前代谢物内经 Robust Rank Aggregation 聚合后的排名，越小越优 |
| `EdgeWeight` | 综合 RRA、相关性、模型支持和筛选支持得到的 0 至 1 边权重，越大越优 |
| `Sign` | `positive` 或 `negative`，取绝对值更强的 Pearson/Spearman 相关方向 |
| `EdgeTier` | 当前表中通常为 `high_confidence` |

`EdgeWeight` 的当前组成：

```text
0.45 * RRAWeight
+ 0.25 * CorrScore
+ 0.20 * ModelScore
+ 0.10 * ScreenScore
```

解释时建议综合使用：

- `EdgeWeight`：总体证据强度。
- `RRARank`：同一代谢物内部的基因优先级。
- `ModelSupportCount`：模型一致性。
- `ScreenSupportCount`：候选筛选一致性。
- `PearsonR`/`SpearmanRho` 和 `Sign`：关联方向及相关强度。

关联不代表因果。该表用于候选优先级和网络探索，不能单独证明调控关系。

### 5.3 `T03_Key_Gene_Summary.csv`

用途：以基因为单位汇总其关联代谢物数量和边权重，一行代表一个基因。

| 字段 | 含义 |
| --- | --- |
| `Gene` | 基因 ID |
| `AssociatedMetaboliteCount` | 总关联网络中该基因关联的不同代谢物数量 |
| `AssociatedMetabolites` | 关联代谢物列表，使用 `|` 分隔 |
| `HighConfidenceMetaboliteCount` | 高置信网络中关联的不同代谢物数量 |
| `HighConfidenceMetabolites` | 高置信代谢物列表，使用 `|` 分隔 |
| `MeanRRARank` | 该基因所有总关联边的平均 RRA 排名 |
| `BestRRARank` | 该基因最优的一条边的 RRA 排名 |
| `MeanEdgeWeight` | 所有关联边的平均边权重 |
| `BestEdgeWeight` | 最强关联边的边权重 |

默认排序优先级：

1. `HighConfidenceMetaboliteCount` 多的基因优先。
2. `AssociatedMetaboliteCount` 多的基因优先。
3. `BestEdgeWeight` 高的基因优先。
4. `BestRRARank` 小的基因优先。

该表适合用于关键基因初筛。需要判断基因具体关联哪个代谢物以及关联方向时，应联查 T02。

### 5.4 `T04_Gene_Module_Assignment.csv`

用途：记录高置信网络基因的模块归属、模块连接性和关联概况。一行代表一个基因。

| 字段 | 含义 |
| --- | --- |
| `Gene` | 基因 ID |
| `Module` | 模块名称；`grey` 表示未归入有效模块 |
| `ModuleColorHex` | 模块显示颜色 |
| `ModuleSize` | 当前模块包含的基因数 |
| `kME` | 基因表达与模块特征向量的相关性，越高越接近模块核心 |
| `IntramodularDegree` | 稀疏模块网络中的模块内连接度 |
| `IsGrey` | 是否属于 grey 模块 |
| `BestEdgeWeight` | 该基因最强基因-代谢物边权重 |
| `AssociatedMetaboliteCount` | 总关联网络中的关联代谢物数量 |
| `HighConfidenceMetaboliteCount` | 高置信网络中的关联代谢物数量 |

Hub gene 常结合较高的 `kME`、较高的 `IntramodularDegree` 和较强的 `BestEdgeWeight` 判断，不建议只依赖单一指标。

### 5.5 `T05_Module_Metabolite_Association.csv`

用途：分析模块特征向量与代谢物丰度之间的关系。一行代表一个模块-代谢物组合。

| 字段 | 含义 |
| --- | --- |
| `Module` | 基因模块名称 |
| `Metabolite` | 代谢物 ID |
| `SpearmanRho` | 模块 eigengene 与代谢物之间的 Spearman 相关系数 |
| `FDR` | 对模块-代谢物相关检验进行多重校正后的值 |

解释：

- `SpearmanRho > 0`：模块整体表达趋势与代谢物同向。
- `SpearmanRho < 0`：模块整体表达趋势与代谢物反向。
- `abs(SpearmanRho)` 越大，单调关联越强。
- 通常结合 `FDR <= 0.05` 判断显著模块-代谢物关联。

### 5.6 `T06_Module_Summary.csv`

用途：一行汇总一个非 grey 模块的规模、核心基因和最强代谢物关联。

| 字段 | 含义 |
| --- | --- |
| `Module` | 模块名称 |
| `ModuleColorHex` | 模块颜色 |
| `ModuleSize` | 模块基因数 |
| `MeanKME` | 模块内基因的平均 kME |
| `MeanIntramodularDegree` | 模块内基因的平均连接度 |
| `TopHubGene` | 按 kME、模块内连接度和边权重综合排序的首位 Hub gene |
| `TopHubKME` | TopHubGene 的 kME |
| `MetaboliteAssociationCount` | `FDR <= 0.05` 的模块-代谢物关联数量 |
| `TopMetabolite` | 当前模块绝对 Spearman 相关最强的代谢物 |
| `TopMetaboliteRho` | TopMetabolite 对应的 Spearman 相关系数 |

该表适合作为模块层面的总览。后续分析通常从 TopHubGene、TopMetabolite 和显著关联数量较多的模块开始。

### 5.7 `T99_Metabolite_Gene_Scoring_Audit.csv`

用途：保存进入建模和综合评分阶段的完整候选边及中间证据，主要用于算法审计、调试和复现。

该表仅在 `export_audit_tables=true` 时生成，Web 平台默认分析通常不导出。

主要字段包括：

```text
Gene
Metabolite
PearsonR / PearsonP / PearsonFDR
SpearmanRho / SpearmanP / SpearmanFDR
MIScore
In_PCC / In_Spearman / In_MI
ScreenSupportCount
ElasticNetScore / ElasticNetRank / ElasticNetSelected
XGBoostScore / XGBoostRank / XGBoostSelected
ModelSupportCount
RRAScore / RRARank / RRAWeight
CorrScore / ModelScore / ScreenScore
EdgeWeight
Sign
TargetK
```

该表数据量可能明显大于 T02，不建议作为普通用户的主要结果表。

## 6. 三个模块结果衔接

推荐的数据衔接关系：

```text
RNA-seq raw count
    |
    v
DEG
    |
    +--> union_significant_genes.csv          候选基因汇总
    |
    +--> union_significant_genes.vst.csv -----+
                                                |
                                                v
                                           GMA transcriptome

Metabolite abundance matrix
    |
    v
DEM
    |
    +--> union_significant_metabolites.csv     候选代谢物汇总
    |
    +--> union_significant_metabolites.matrix.csv
                                                |
                                                v
                                           GMA metabolome
```

关键区别：

| 文件 | 是否已变换 | 进入 GMA 时建议 |
| --- | --- | --- |
| `union_significant_genes.vst.csv` | 已做 VST | 关闭 transcriptome Log2 |
| `union_significant_metabolites.matrix.csv` | 保存原始对齐矩阵子集 | 根据原始数据尺度决定是否开启 metabolome Log2 |

## 7. 结果解读建议

### DEG

1. 先看 `differential_gene_counts.csv` 了解每个 contrast 的整体规模。
2. 使用 `{contrast}.sig.csv` 查看具体显著基因和方向。
3. 使用 `union_significant_genes.csv` 查找跨 contrast 重复出现的候选。
4. 需要进入 GMA 时使用 VST 矩阵，而不是显著性汇总表。

### DEM

1. 先看 `differential_metabolite_counts.csv`。
2. 对显著代谢物同时检查 `padj_bh`、`log2FoldChange` 和 `vip`。
3. 使用 `n_tested`、`n_reference` 识别缺失观测过多的候选。
4. 使用 OPLS-DA scores 表检查样本分离，但不要把样本得分当作代谢物重要性。

### GMA

1. 使用 T01 从代谢物层面了解关联规模。
2. 使用 T02 查看具体高置信边。
3. 使用 T03 筛选关联代谢物较多且边权重较高的关键基因。
4. 使用 T04-T06 从模块层面寻找 Hub gene 和模块-代谢物关系。
5. 关联结果需要结合实验设计、已有生物学证据和后续实验验证，不应直接解释为因果调控。

## 8. 常见疑问

### 为什么显著表可能为空？

可能原因包括样本量不足、组间差异较弱、校正后 P 值不显著、fold change 未达到阈值，或 DEM 的 VIP 未达到阈值。

### 为什么 `all.csv` 的行数少于原始输入特征数？

低总 count、缺失率过高、无变化特征或模型无法使用的特征可能在分析前或分析中被过滤。

### 为什么并集表没有 Up/Down？

一个特征在不同 contrast 中可能方向不同。并集表只汇总显著出现次数和最佳统计量，方向必须回查每个 contrast 的结果表。

### 为什么 GMA 的 T04-T06 不存在？

模块分析依赖高置信网络。如果高置信边为空、可用基因少于 2 个，或者基因无法形成有效模块，模块表不会生成。

### `EdgeWeight` 是否等于相关系数？

不是。`EdgeWeight` 是融合 RRA、相关性、机器学习模型支持和候选筛选支持的综合分数。具体线性相关或秩相关强度应查看 `PearsonR` 和 `SpearmanRho`。

## 9. 对接和版本管理要求

结果表属于平台、算法和下游分析之间的接口契约。以下修改需要同步更新本文档和对接代码：

- 文件名变化。
- 字段新增、删除或重命名。
- 显著性规则变化。
- 输出矩阵是否经过变换的变化。
- GMA EdgeWeight 计算方式变化。
- 模块检测方法或模块表字段变化。

下游程序应优先按字段名读取，不要依赖固定列序号。
