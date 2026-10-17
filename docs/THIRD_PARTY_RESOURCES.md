# 本地参考资源

公开版不包含原 v8 的患者级 RNA、蛋白、代谢、临床或 TCGA 输入，也不包含原研究的基因映射、基因集、KEGG 表和文献缓存。这些数据不随 GPL 源码许可一起授权。

自然语言任务可直接使用自己的 CSV、TSV、RDS、GMT。将其复制到本机项目的 `data/raw/`、`data/metadata/`、`data/signature/` 或 `knowledge/resources/`；这些目录被 Git 忽略，更新器不管理其内容。不使用符号链接或项目外路径绕过输入边界。

如需参考资源，请自行从原始发布方获取并遵守其条款：

- MSigDB：<https://www.gsea-msigdb.org/gsea/msigdb/>。
- org.Hs.eg.db：<https://bioconductor.org/packages/org.Hs.eg.db/>。
- KEGG：<https://www.kegg.jp/kegg/legal.html>。

记录下载来源、版本、日期和校验和；选择实际需要的资源，无需恢复原研究队列。原固定研究流程依赖特定数据适配器，不是公开版支持入口。Python/R 依赖通过安装器从上游安装，各依赖保留自己的许可证；公开包不捆绑其运行库或二进制。
