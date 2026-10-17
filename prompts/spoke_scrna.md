# Single-cell RNA (external atlas context)
只读外部泛癌图谱对象（用户确认范围）；不重新聚类、不重新注释、不重过滤，仅描述性QC并记录。
细胞类型用对象提供的Maintypes；统计单位是供者样本：样本×细胞类型pseudobulk（counts→CPM→log1p），主检验为供者级配对Wilcoxon（该细胞类型 vs 同样本其他细胞类型均值），BH按预定义族。
细胞级presto统计只作描述性支持，明确细胞非独立单位。
候选基因在查询图谱前冻结：预设轴基因＋bulk DE selected（FDR与效应阈值为运行冻结值）；不按图谱结果回调任何阈值。
DE基因集ssGSEA在样本级pseudobulk上计算，输出全部细胞类型评分，含阴性和反向。
去卷积为NNLS估计（泛癌签名主分析；HCC来源签名与top50 marker为预先声明敏感性），输出全部面板与配对limma检验；恶性/上皮签名向HCC bulk迁移不确定必须标注。
所有输出标记external_context/external_atlas；供者与本队列不同，禁止样本级关联，不计入两种测量组学规则，不授予独立验证或机制置信度。
按来源（Source）输出轴基因描述性表达表，透明呈现癌种混杂。
