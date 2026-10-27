# 本地参考资源与内置数据集

公开版**不包含**原 v8 研究的患者级 RNA、蛋白、代谢或临床队列输入，也不包含原研究的基因映射、基因集、KEGG 表和文献缓存。这些数据不随 GPL 源码许可一起授权。

公开版**包含**一份独立的开放获取数据集，用于演示和验证自然语言任务：

- `datasets/TCGA-LIHC/`：从 **GDC（Genomic Data Commons）** 下载的 TCGA-LIHC 肝细胞癌 bulk RNA-seq，STAR - Counts 流程、GENCODE v36，19938 个 protein_coding 基因 × 424 个样本（371 原发瘤、50 癌旁、3 复发瘤，含 50 对肿瘤/癌旁）。
- 来源、访问级别、数据版本（Data Release 46.0）与上游流程记录在 `datasets/TCGA-LIHC/dataset.json` 的 `source` 字段，`provenance_status` 为 `recorded`。逐文件 md5 与字节数见同文件的 `upstream_files`（424 条）。
- 访问级别为 **open**（TCGA 开放获取层），不属于受控访问的 dbGaP 数据。
- 该数据集**不适用** GPL-3.0-or-later：代码许可以 `LICENSE` 为准，数据按上游 GDC 数据使用条款分发。本仓库未额外附加任何患者信息，仅保留 GDC 公开的 TCGA sample barcode。

自然语言任务可直接使用自己的 CSV、TSV、RDS、GMT。将其复制到本机项目的 `data/raw/`、`data/metadata/`、`data/signature/` 或 `knowledge/resources/`；这些目录被 Git 忽略，更新器不管理其内容。不使用符号链接或项目外路径绕过输入边界。安装和 `/cd` 新建工作区时，内置数据集会自动复制到 `data/raw/TCGA-LIHC/` 与 `data/metadata/TCGA-LIHC/`，已有文件不会被覆盖。

如需其他参考资源，请自行从原始发布方获取并遵守其条款：

- TCGA / GDC：<https://portal.gdc.cancer.gov/>、<https://gdc.cancer.gov/about-data/data-use-agreement>。
- MSigDB：<https://www.gsea-msigdb.org/gsea/msigdb/>。
- org.Hs.eg.db：<https://bioconductor.org/packages/org.Hs.eg.db/>。
- KEGG：<https://www.kegg.jp/kegg/legal.html>。

记录下载来源、版本、日期和校验和；选择实际需要的资源，无需恢复原研究队列。原固定研究流程依赖特定数据适配器，不是公开版支持入口。Python/R 依赖通过安装器从上游安装，各依赖保留自己的许可证；公开包不捆绑其运行库或二进制。
