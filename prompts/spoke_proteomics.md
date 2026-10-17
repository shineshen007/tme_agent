# Proteomics
输入是完整的log2、median-centered基因层矩阵；不套用MaxQuant原始蛋白组规则，不重复log，不插补。
limma对患者tumor-adjacent差值拟合截距模型，分别分析159和109人，报告相对定量尺度。
median centering方向未知作为限制保留。输出统一de字段。
RNA-蛋白关联交由整合节点；一个基因的两个汇总log2FC不能计算相关。
