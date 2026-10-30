# 通路评分触发协议（GSVA 多方法）

本文件说明 `tme_agent` 的**通路评分触发关键词**、冻结协议、五张输出表和独立校验器的
能力边界。协议本身由程序生成并写入批准的计划哈希，模型不能改写方法、阈值或共识规则。

## 1. 触发关键词

请求文本命中下列任一关键词（大小写不敏感的子串匹配），且计划确实声明要计算基因集评分时，
系统才进入该协议：

| 默认关键词 | 说明 |
| --- | --- |
| `GSVA`、`ssGSEA`、`PLAGE`、`ZSCORE` | 方法名 |
| `通路评分`、`通路得分`、`基因集评分` | 中文表述 |
| `pathway score`、`gene set score` | 英文表述 |

关键词列表可在 `config/study_config.yaml` 覆盖：

```yaml
analysis:
  pathway_scoring:
    trigger: [gsva, ssgsea, plage, zscore, 通路评分, 通路得分, 基因集评分, pathway score, gene set score]
    methods: [ssgsea, plage, zscore, gsva]
    adjusted_p_max: 0.05
    consensus_min_methods: 2
    min_size: 5
    max_size: 500
    seed: 2024
```

触发是**字符串匹配**，不是语义理解。命中但计划没有声明评分步骤时，计划会带一条可见限制
`pathway_scoring_trigger_not_implemented`，不会强行改造计划。

## 2. 方法集合

- 默认四种全跑：`ssgsea`、`plage`、`zscore`、`gsva`，分别对应
  `GSVA::ssgseaParam`、`GSVA::plageParam`、`GSVA::zscoreParam`、`GSVA::gsvaParam`，
  统一经 `GSVA::gsva(param, verbose=FALSE)` 计算。
- 用户在请求中**显式只点名一种**方法时，只跑该种（单方法时跨方法一致性规则自动失效）。
- 点名两种及以上视为要求全部四种。

评分前必须先剔除跨样本零方差的基因并记录剔除数量：`plage`、`zscore`、`gsva` 遇到这类基因会
整体报错（`subscript out of bounds`），`ssgsea` 虽能容忍，但为可比性统一使用过滤后的同一矩阵。

## 3. 显著性规则

- **每种方法各自**按冻结的校正 p 阈值判定，BH 校正的范围是**该方法自己的基因集家族**
  （`family = pathway_scoring_<method>`）。
- 额外给出**跨方法一致性计数**（`methods_significant`、`significant_methods`、
  `direction_consistent`、`consensus`）。
- **不做**跨方法分数合并、平均、排名合并或 z 合并：四种方法的量纲不同，合并会产生无意义的数。
- 阈值来源（按优先级，均不可由模型编造）：
  1. 请求中显式写出的阈值（作为精确引用参数记录，`source=user`）；
  2. 配置 `analysis.pathway_scoring.adjusted_p_max`（`source=config:...`）；
  3. 两者都没有 → 规划失败并提示补配置或写明阈值，**不会静默使用默认值**。

## 4. 输出表

路径由协议冻结，全部位于 `analysis/<task_id>/` 下。

| 文件 | 内容 |
| --- | --- |
| `tables/gsva_scores.csv` | 样本级得分长表：`gene_set, sample_id, score, method`，唯一键 `(method, gene_set, sample_id)` |
| `tables/gsva_group_tests.csv` | 每方法每基因集的组间检验：`family, test, contrast_numerator, contrast_denominator, direction, n_units, n_numerator, n_denominator, effect, ci_low, ci_high, pvalue, padj, selected, status, reason, effect_ci_method, pvalue_method` |
| `tables/gsva_significant_pathways.csv` | 跨方法汇总：`methods_tested, methods_significant, significant_methods, min_padj, max_abs_effect, direction_consistent, n_units, selected, consensus, status, reason` |
| `tables/gsva_gene_set_audit.csv` | 基因集审计：`size_in_resource, size_in_matrix, status, reason`（`used`/`size_below_min`/`size_above_max`/`zero_overlap`/`no_variable_features`） |
| `reports/gsva_protocol.json` | 实际执行的协议、GSVA/R 版本、`sessionInfo`、种子、剔除特征数、剔除基因集、警告 |

约定：

- `status ∈ {tested, not_estimable}`。不可估计时数值列留空，`reason` 取自
  `zero_difference`、`fewer_than_three_units`、`nonfinite_estimate`、`wilcox_failed`。
- 阴性通路和不可估计通路都保留，不删除、不隐藏。
- 审计表必须覆盖基因集资源中的**全部**集合各一次：`size_in_resource` 为资源大小；
  `size_in_matrix` 为通过协议零方差过滤、实际进入评分的矩阵成员数；状态优先级为
  `zero_overlap` → `no_variable_features` → `size_below_min` → `size_above_max` → `used`。
  非 `used` 行必须有非空 `reason`，`used` 行必须留空。
- 批准时会把基因集资源名称哈希、资源哈希、每个集合的资源/矩阵大小和状态冻结进
  `pathway_protocol.gene_set_universe`；校验器从可信输入 inspection 重新派生并逐项比较。
  因此同时从所有输出表删除通路、改变过滤理由或缩小 BH 家族都会失败。
- 全部通路均不可估计时不进行无意义的 padj 范围检查；结果保留并返回
  `statistical_status=not_estimable`，不升级为科学结论。
- `selected = (status=="tested") & (padj <= 冻结阈值)`；
  `consensus = selected & (methods_significant >= consensus_min_methods) & direction_consistent`。
- 未给出显式对比方向时，`direction="unknown"`，`direction_consistent` 留空，`consensus`
  只依赖显著性与计数，并在计划限制里注明。

## 5. 校验器能证明什么、不能证明什么

任务结束前，`orchestrator/pathway_scoring.py::verify_outputs` 会从**样本级得分表**独立重算：

- 覆盖完整性：方法 × 基因集 × 样本与冻结绑定完全一致，无重复键，无静默丢样本或丢基因集；
- 每行秩检验 p 值（`scipy.stats.wilcoxon` / `mannwhitneyu`，`method="approx"`，
  连续性校正与 R 的 `wilcox.test(exact=FALSE, correct=TRUE)` 一致，容差 `1e-8`）；
- 每个方法家族内的 BH（`scipy.stats.false_discovery_control(method="bh")`）；
- `selected`、`methods_tested`、`methods_significant`、`significant_methods`、
  `min_padj`、`max_abs_effect`、`direction_consistent`、`consensus`；
- 状态/原因词表、`n_units >= 3`、实际 tested 行的 `padj ∈ [0,1]`（无可估计行时跳过范围检查）、效应量落在自身 CI 内、显著表覆盖全部已评分通路；
- `gsva_protocol.json` 的方法、阈值、检验、对比、种子与计划冻结值一致。

**能证明**：表内自洽、检验与校正规则正确、覆盖完整、显著性没有被手工改写或删行掩盖。

**不能证明**：得分矩阵确实由点名的 GSVA 方法算出。方法真实性由
`scripts/32_gsva_multimethod_reference.R` 在合成数据上用四个参数对象重算并逐方法比对
（容差 `1e-8`）来验真；该脚本只用于测试与验收，不进入任务运行路径。工程校验通过不等于
科学结论成立。

## 6. 分数含义与解释边界

GSVA 的四种方法给出的是**相对富集分**，量纲与方法相关（实测 `ssgsea`/`plage`/`gsva`
大致在 ±0.6 内，`zscore` 在 ±2 内）。因此：

- 分数不是细胞比例、代谢通量、酶活性或通路"被激活"的直接测量；
- 不同方法的分数不可直接比较大小或平均；
- 显著性只说明该基因集在当前设计与样本量下与分组共变，不构成因果证据；
- 结论强度受样本量、配对结构、基因集来源与覆盖度限制；`n_units` 必须按独立生物学单位报告。

## 7. 相关代码

| 位置 | 作用 |
| --- | --- |
| `orchestrator/pathway_scoring.py` | 触发识别、协议冻结、计划强制、预览渲染、独立校验器 |
| `multiomics_tui/task_planner.py` | 规划期注入协议提示、冻结阈值参数、写入 `plan_hash`、预览披露 |
| `multiomics_tui/task_runner.py` | 生成期协议提示、运行后调用校验器并写入 `result_manifest.json` |
| `scripts/32_gsva_multimethod_reference.R` | 合成数据上的方法真实性参考验真 |
| `tests/test_pathway_scoring.py` | 触发/协议/计划闸门/校验器/真实 R 测试 |

## 8. 常见问题

**想只跑一种方法？** 在请求里明确写该方法名，例如"只用 plage 算法计算通路评分"。

**想改显著性阈值？** 在请求里写出来（例如"FDR 0.01"），或用本地
`config/study_config.yaml` 的 `analysis.pathway_scoring.adjusted_p_max`。

**为什么没有 `-log10FDR` 的阈值被采纳？** 那是图形变换标注，不是显著性水平；解析器只接受
(0,1) 内的概率值（`FDR 5%` 视为 0.05）。

**为什么某个基因集出现在审计表却不在得分表？** 它的大小超出冻结的 `minSize/maxSize`，
或与表达矩阵零重叠，或全部成员零方差；审计表会给出 `status` 与 `reason`，不会被静默丢弃。

**为什么某通路只有部分方法有结果？** 该通路在其他方法下不可估计（例如配对差值恒为 0），
`methods_tested` 与 `methods_significant` 会如实反映，不补齐、不推断。
