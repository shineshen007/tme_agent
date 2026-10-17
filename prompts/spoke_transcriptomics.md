# Transcriptomics
使用校验后的非负整数counts，排除已登记的全NA非基因行，应用四个授权别名。
DESeq2配对模型 ~ patient_id + tissue；reference=adjacent。159人和109人分别拟合。
保留全部特征的统计/过滤状态及95%CI。表达输出为log2(normalized counts+1)，与log2FC区分。
GSEA使用完整可检验排序，不只输入DEG。注释映射固定版本并记录歧义。
ssGSEA报告免疫表达程序分数及剔除轴基因的敏感性，不声明细胞比例。
