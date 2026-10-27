# TCGA-LIHC 内置数据集

肝细胞癌（LIHC）TCGA 开放获取 bulk RNA-seq，来自 **GDC（Genomic Data Commons）官方 STAR - Counts 流程**。
本目录是结构化、压缩后的规范副本：不重算、不重新归一化、不对 TPM 取整。

- 数据集 ID：`TCGA-LIHC`
- 清单：`dataset.json`（GDC 文件 md5、维度、分组、冻结绑定、质量注记）
- 来源状态：`provenance_status: recorded` — 来源、访问级别、数据版本与上游流程均已记录

## 来源

| 字段 | 值 |
| --- | --- |
| 仓库 | GDC，<https://portal.gdc.cancer.gov/> |
| 项目 | `TCGA-LIHC` |
| 访问级别 | **open**（开放获取，非受控 dbGaP） |
| 数据版本 | Data Release 46.0（2026-08-10），GDC API 8.5.0 |
| 上游流程 | **STAR - Counts**（GDC RNA-Seq Alignment） |
| 基因模型 | **GENCODE v36** |
| 表达列 | counts = `unstranded`，TPM = `tpm_unstranded` |

数据条款按上游 GDC 要求执行；**GPL-3.0-or-later 只覆盖读取本数据的代码，不覆盖数据本身**。
逐个文件的 md5 与字节数记录在 `dataset.json` 的 `upstream_files`（424 条），可逐文件回查 GDC。

## 文件

| 文件 | 内容 | 大小 |
| --- | --- | --- |
| `expression/counts_full.rds` | 19938 基因 × 424 样本，integer 未标准化 counts | 11.5 MB |
| `expression/counts_paired.rds` | 19938 基因 × 100 样本，50 对肿瘤/癌旁，列序同 `samples_paired.csv` | 2.7 MB |
| `expression/tpm_full.rds` | 19938 基因 × 424 样本，GDC `tpm_unstranded` 原值 | 20.3 MB |
| `expression/gene_annotation.csv` | 19938 行：`gene_id`/`gene_name`/`gene_type` | 0.9 MB |
| `metadata/samples_full.csv` | 424 行样本表 | 52 KB |
| `metadata/samples_paired.csv` | 100 行配对样本表 | 13 KB |

矩阵为 RDS，行名是基因符号、列名是 **16 位 TCGA sample barcode**（如 `TCGA-ES-A2HS-01A`）。
用 `utils::readRDS()` 直接得到带 dimnames 的 matrix。

**样本 ID 格式注意**：GDC 用 16 位 sample barcode，而 TCGA 早期文件常用 28 位 aliquot barcode
（`TCGA-ES-A2HS-01A-11R-A180-07`）。本数据集统一用 16 位；`file_id` 列可回查 GDC 原文件。
如需 28 位 aliquot，请从 GDC 按 `file_id` 取原 TSV。

## 基因范围与重复符号

只保留 **protein_coding**（GENCODE v36 中 19,962 行 → **19,938 个唯一符号**）。
`gene_annotation.csv` 给出每行的代表性 `gene_id` 与 `gene_type`，可据此自行扩展到其它基因类型。

GENCODE v36 中有 **24 个基因符号重复**（PAR 基因等，如 `CD99`、`MATR3`、`POLR2J3`、`TMSB15B`）。
本数据集对同名行**求和**，因此矩阵一行 = 该符号所有 gene_id 之和。

> 这一点与 TCGA 早期派生表不同：若按"只取第一行"处理，`MATR3` 会少 1、`POLR2J3` 会少 4、
> `TMSB15B` 会少 2 counts（实测）。求和是 GENCODE 语义下更完整的选择。

## 队列构成

| 组 | 数量 | GDC `sample_type` |
| --- | --- | --- |
| 原发瘤 Tumor | 371 | `Primary Tumor` |
| 癌旁 Non_Tumor | 50 | `Solid Tissue Normal` |
| 复发瘤 Recurrent_Tumor | 3 | `Recurrent Tumor` |
| 配对患者（原发+癌旁） | 50 | `pair_id` 非空 |

`TCGA-DD-AACA` 有 3 个样本（1 原发 + 2 复发），`TCGA-ZS-A9CF` 有 2 个（1 原发 + 1 复发）。
只做肿瘤/癌旁配对分析时用 `counts_paired.rds`；3 例复发瘤不进配对子集，但不会被从全量中静默排除。

## 已冻结的输入绑定

直接作为计划里的 `input_bindings.matrices` 条目使用（`/datasets` 会打印同样的 JSON）：

```json
{"path": "data/raw/TCGA-LIHC/counts_paired.rds", "sample_axis": "columns",
 "metadata_path": "data/metadata/TCGA-LIHC/samples_paired.csv", "sample_key": "sample_id",
 "group_key": "group", "group_map": {"Tumor": "Tumor", "Non_Tumor": "Non_Tumor"},
 "pair_key": "pair_id", "preprocessing": "none"}
```

全量队列（371/50/3，无配对）用 `counts_full.rds` 或 `tpm_full.rds` + `samples_full.csv`，
`group_map` 覆盖三个水平，`pair_key` 为 `null`。不要对全量表填 `pair_key`：
`pair_id` 在未配对样本上是 `NA`，绑定会因配对字段不完整被拒绝。

## 示例请求

- 配对差异：`用内置 TCGA-LIHC 的 counts_paired，按 pair_id 做配对 Wilcoxon 检验比较肿瘤与癌旁，输出完整结果表和火山图。`
- 通路评分：`用 counts_full 计算 GSVA PLAGE 评分，按 group 比较肿瘤与癌旁，FDR 分别校正。`
- 描述性检查：`检查 samples_full 的缺失值、分组计数与样本类型分布，不做组间推断。`

## 已知限制（不要据此外推）

- **TPM 列和是 776,405–978,224，不是 1e6**。原因：GDC 的 `tpm_unstranded` 在全转录组上归一化到
  1e6，本数据集只保留 protein_coding 子集（约占 78%–97%，随样本非编码占比浮动）。
  **不要重新缩放到 1e6，也不要与其它 TPM 数据集直接合并**——除非对方也是同样口径。
  需要真正的 1e6 口径时，请从 GDC 原 TSV 取全部基因类型。
- counts 为 unstranded read counts，本目录不做文库大小标准化；差异表达方法自行处理。
- 本数据集**不包含**临床诊断表（分期、生存、病理等）。只含样本级字段（`sample_type`、
  `tissue_type`、`patient`、`group`、`pairing`、`pair_id`、`file_id`）。需要临床终点请从 GDC
  `cases` 接口或 Clinical Supplement 另行获取，并注意其自身版本。
- 单队列、观察性数据；任何生存/预后关联都只是本队列内的关联证据。
- 复发瘤样本无配对癌旁，配对分析中不可用。

## 重建

```bash
# 1. 从 GDC 下载 424 个 STAR counts 文件并逐文件校验 md5（约 410 MB，5–6 分钟）
python3 scripts/08_fetch_gdc_tcga_lihc.py --project-root .

# 2. 只校验缓存，不联网
python3 scripts/08_fetch_gdc_tcga_lihc.py --verify-only

# 3. 由缓存构建数据集
Rscript --vanilla scripts/07_build_builtin_dataset.R . 2026-10-26T11:15:00+0800
```

缓存位于 `data/raw/_gdc_cache/`（Git 忽略）。同一 GDC 文件 + 同一 R 版本 → 相同 `.rds` 字节；
`dataset.json` 中 `build.built_at` 是唯一随构建时间变化的字段。安装后数据文件复制到工作区的
`data/raw/TCGA-LIHC/` 与 `data/metadata/TCGA-LIHC/`，已有文件不会被覆盖。

GDC 会随数据版本更新重算定量。若 `fetch_report.json` 的 `data_release`/`gdc_commit` 或文件 md5 变化，
重建结果会不同——这是上游变更，不是本地错误，需显式更新清单。
