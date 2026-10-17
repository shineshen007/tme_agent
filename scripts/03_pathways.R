args <- commandArgs(trailingOnly=TRUE)
job <- jsonlite::fromJSON(args[1])
set.seed(job$seed)
out <- job$output_dir
dir.create(out,recursive=TRUE,showWarnings=FALSE)
for(p in c('fgsea','GSVA','BiocParallel')) if(!requireNamespace(p,quietly=TRUE)) stop('Missing package: ',p)
r <- read.csv(file.path(job$statistics_dir,'de.csv'),check.names=FALSE)
e <- as.matrix(read.csv(file.path(job$statistics_dir,'expression.csv'),row.names=1,check.names=FALSE))
meta <- read.csv(file.path(job$statistics_dir,'sample_metadata.csv'),check.names=FALSE)
map <- read.csv(job$mapping,stringsAsFactors=FALSE)
if(job$omics=='transcriptomics') {
  distinct <- unique(map[,c('ENSEMBL','SYMBOL')])
  distinct <- distinct[!is.na(distinct$SYMBOL) & distinct$SYMBOL!='',]
  multiplicity <- table(distinct$ENSEMBL)
  distinct <- distinct[distinct$ENSEMBL %in% names(multiplicity)[multiplicity==1],]
  symbols <- distinct$SYMBOL[match(r$feature_id,distinct$ENSEMBL)]
} else symbols <- r$feature_id
r$symbol <- symbols
# Stable abundance-based representative, selected independently of DE significance.
o <- order(-r$mean_abundance,r$feature_id,na.last=TRUE)
chosen <- r[o,];chosen <- chosen[!is.na(chosen$symbol) & chosen$symbol!='',]
chosen <- chosen[!duplicated(chosen$symbol),]
write.csv(data.frame(feature_id=r$feature_id,symbol=r$symbol,selected_representative=r$feature_id %in% chosen$feature_id),file.path(out,'gene_mapping_audit.csv'),row.names=FALSE)
pathways <- fgsea::gmtPathways(job$gmt)
valid <- is.finite(chosen$statistic)
ranks <- chosen$statistic[valid];names(ranks) <- chosen$symbol[valid]
ranks <- sort(ranks,decreasing=TRUE)
gsea <- as.data.frame(fgsea::fgseaMultilevel(pathways,ranks,minSize=job$min_size,maxSize=job$max_size,
                                           eps=0,nPermSimple=1000,BPPARAM=BiocParallel::SerialParam()))
gsea$leadingEdge <- vapply(gsea$leadingEdge,paste,collapse=';',character(1))
gsea$omics <- job$omics;gsea$cohort_id <- job$cohort_id
write.csv(gsea,file.path(out,'gsea_full.csv'),row.names=FALSE)
write.csv(data.frame(symbol=names(ranks),rank_statistic=ranks),file.path(out,'gsea_rank_source.csv'),row.names=FALSE)
if(job$omics=='transcriptomics') {
  ss <- chosen[chosen$feature_id %in% rownames(e),]
  em <- e[ss$feature_id,,drop=FALSE];rownames(em) <- ss$symbol
  immune <- fgsea::gmtPathways(job$immune_gmt)
  for(version in c('original','axis_genes_excluded')) {
    sets <- immune
    if(version=='axis_genes_excluded') sets <- lapply(sets,setdiff,y=job$axis_genes)
    score <- GSVA::gsva(GSVA::ssgseaParam(em,sets,minSize=job$min_size,normalize=TRUE),verbose=FALSE,BPPARAM=BiocParallel::SerialParam())
    write.csv(data.frame(program=rownames(score),score,check.names=FALSE),file.path(out,paste0('immune_scores_',version,'.csv')),row.names=FALSE)
    pairs <- split(meta,meta$patient_id)
    delta <- sapply(pairs,function(p)score[,p$biospecimen_id[p$tissue=='tumor']]-score[,p$biospecimen_id[p$tissue=='adjacent']])
    rownames(delta) <- rownames(score)
    write.csv(data.frame(program=rownames(delta),delta,check.names=FALSE),file.path(out,paste0('immune_delta_',version,'.csv')),row.names=FALSE)
    n <- ncol(delta);effect <- rowMeans(delta);se <- apply(delta,1,sd)/sqrt(n)
    p <- ifelse(se>0,2*pt(-abs(effect/se),df=n-1),NA_real_)
    ir <- data.frame(program=rownames(delta),effect=effect,ci_low=effect-qt(.975,n-1)*se,ci_high=effect+qt(.975,n-1)*se,pvalue=p,padj=p.adjust(p,'BH'),n_pairs=n,method='paired_t',score_type='ssGSEA_expression_program_not_cell_fraction',source_omics='transcriptomics')
    write.csv(ir,file.path(out,paste0('immune_de_',version,'.csv')),row.names=FALSE)
  }
}
jsonlite::write_json(list(status='complete',n_pathways=nrow(gsea),seed=job$seed,
  interpretation='Exploratory enrichment on full tested ranking; immune scores share the RNA evidence source'),file.path(out,'pathways.json'),pretty=TRUE,auto_unbox=TRUE)
writeLines(capture.output(sessionInfo()),file.path(out,'sessionInfo.txt'))
