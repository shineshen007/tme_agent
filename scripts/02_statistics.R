# Deterministic paired statistical engine. Called with a frozen JSON job.
args <- commandArgs(trailingOnly=TRUE)
if(length(args)!=1L) stop('Usage: Rscript --vanilla scripts/02_statistics.R JOB.json')
if(!requireNamespace('jsonlite',quietly=TRUE)) stop('jsonlite unavailable')
job <- jsonlite::fromJSON(args[1], simplifyVector=TRUE)
if(length(job$r_library_paths)) .libPaths(c(job$r_library_paths,.libPaths()))
out <- job$output_dir
dir.create(out,recursive=TRUE,showWarnings=FALSE)
set.seed(job$seed)
options(warn=1)
writeLines(capture.output(sessionInfo()),file.path(out,'sessionInfo_start.txt'))
required <- if(job$omics=='transcriptomics') c('DESeq2','limma') else 'limma'
for(p in required) if(!requireNamespace(p,quietly=TRUE)) stop('Missing package: ',p)
matrix_file <- function(x,path) write.csv(data.frame(feature_id=rownames(x),x,check.names=FALSE),path,row.names=FALSE)
m <- read.csv(job$matrix, check.names=FALSE, row.names=1)
m <- as.matrix(m); storage.mode(m) <- 'double'
meta <- read.csv(job$metadata,check.names=FALSE,stringsAsFactors=FALSE)
meta <- meta[meta$omics==job$omics,,drop=FALSE]
if(job$cohort_id=='triple109') meta <- meta[tolower(meta$in_triple109)=='true',,drop=FALSE]
if(job$cohort_id=='same_batch108') {
  same <- names(which(tapply(meta$batch,meta$patient_id,function(x)length(unique(x)))==1L))
  meta <- meta[meta$patient_id %in% same,,drop=FALSE]
}
meta <- meta[order(meta$patient_id,meta$tissue),,drop=FALSE]
stopifnot(!anyDuplicated(meta$biospecimen_id),all(meta$biospecimen_id %in% colnames(m)))
m <- m[,meta$biospecimen_id,drop=FALSE]
stopifnot(all(is.finite(m)),ncol(m)==nrow(meta))
pairs <- split(meta,meta$patient_id)
if(any(vapply(pairs,function(x)nrow(x)!=2L || !setequal(x$tissue,c('tumor','adjacent')),logical(1)))) stop('Invalid pair')
if(length(pairs)<job$min_pairs) stop('Insufficient biological pairs')
meta$patient_id <- factor(meta$patient_id)
meta$tissue <- factor(meta$tissue,levels=c('adjacent','tumor'))
rownames(meta) <- meta$biospecimen_id
design <- model.matrix(~patient_id+tissue,meta)
stopifnot(qr(design)$rank==ncol(design))
all_ids <- rownames(m)
filter_reason <- rep('',nrow(m)); names(filter_reason) <- all_ids
if(job$omics=='transcriptomics') {
  stopifnot(all(m>=0),all(m==floor(m)))
  keep <- rowSums(m)>=job$min_total_count
  filter_reason[!keep] <- 'low_total_count'
  dds <- DESeq2::DESeqDataSetFromMatrix(round(m[keep,,drop=FALSE]),meta,~patient_id+tissue)
  checkpoint <- file.path(out,'dds_fitted.rds')
  # Restart within this immutable job only. Python verifies fingerprint and job hash first.
  if(file.exists(checkpoint)) {
    checksum <- paste0(checkpoint,'.md5')
    if(!file.exists(checksum) || !identical(unname(tools::md5sum(checkpoint)),readLines(checksum))) stop('Checkpoint integrity failed; preserve and inspect checkpoint before restarting')
    dds <- readRDS(checkpoint)
    stopifnot(identical(colnames(dds),colnames(m)),identical(rownames(dds),rownames(m)[keep]))
  } else {
    if(!is.null(job$r_workers) && job$r_workers>1L) {
      source(job$checkpoint_script)
      dds <- fit_dds_checkpointed(dds,out,job$r_workers,job$r_chunk_size,job$seed,job$fit_type)
    } else dds <- DESeq2::DESeq(dds,fitType=job$fit_type,minReplicatesForReplace=Inf,parallel=FALSE)
    saveRDS(dds,checkpoint)
    writeLines(unname(tools::md5sum(checkpoint)),paste0(checkpoint,'.md5'))
  }
  res <- as.data.frame(DESeq2::results(dds,contrast=c('tissue','tumor','adjacent'),alpha=job$alpha))
  diagnostics <- as.data.frame(S4Vectors::mcols(dds))
  available <- intersect(c('betaConv','betaIter','dispGeneIter','dispIter'),names(diagnostics))
  write.csv(data.frame(feature_id=rownames(dds),diagnostics[,available,drop=FALSE]),file.path(out,'model_diagnostics.csv'),row.names=FALSE)
  nonconverged <- !diagnostics$betaConv
  n_nc <- sum(nonconverged); n_est <- sum(!is.na(diagnostics$betaConv))
  frac_nc <- n_nc / max(n_est, 1)
  jsonlite::write_json(list(dispersion_fit_requested=job$fit_type,dispersion_fit_actual=attr(DESeq2::dispersionFunction(dds),'fitType'),
    n_beta_nonconverged=n_nc,n_genes=n_est,nonconverged_fraction=frac_nc,
    max_nonconverged_fraction=job$max_nonconverged_fraction,
    convergence_policy='nonconverged genes are retained with NA effect/pvalue/padj and status not_tested_nonconverged; they cannot be selected; global stop only if fraction exceeds the pre-specified pathological limit',
    workers=if(is.null(job$r_workers)) 1 else job$r_workers,blas=unname(extSoftVersion()['BLAS'])),
    file.path(out,'model_status.json'),auto_unbox=TRUE,pretty=TRUE)
  if(frac_nc > job$max_nonconverged_fraction)
    stop(sprintf('Pathological nonconvergence: %d/%d genes (%.2f%%) exceed the pre-specified limit %.3f; inspect model_diagnostics.csv',n_nc,n_est,100*frac_nc,job$max_nonconverged_fraction))
  r <- data.frame(feature_id=rownames(res),effect=res$log2FoldChange,se=res$lfcSE,
                  statistic=res$stat,pvalue=res$pvalue,padj=res$padj,
                  ci_low=res$log2FoldChange-qnorm(.975)*res$lfcSE,
                  ci_high=res$log2FoldChange+qnorm(.975)*res$lfcSE,
                  mean_abundance=res$baseMean,df=NA_real_,stringsAsFactors=FALSE)
  r$test_status <- ifelse(is.na(r$pvalue),'not_tested_outlier_or_zero',ifelse(is.na(r$padj),'independent_filtered','tested'))
  # Standard DESeq2 handling: genes whose coefficients did not converge are reported as not tested,
  # retained in the full table with NA effect/CI/p, and excluded from selection. Thresholds unchanged.
  r$converged <- !(r$feature_id %in% rownames(diagnostics)[nonconverged])
  nc_rows <- !r$converged
  r[nc_rows, c('effect','se','statistic','pvalue','padj','ci_low','ci_high')] <- NA_real_
  r$test_status[nc_rows] <- 'not_tested_nonconverged'
  expression <- log2(DESeq2::counts(dds,normalized=TRUE)+1)
  expression_scale <- 'log2_size_factor_normalized_count_plus_1'
  method <- paste0('DESeq2_',packageVersion('DESeq2'),'_paired_Wald')
} else {
  expression <- if(job$omics=='metabolomics') log2(m) else m
  expression_scale <- if(job$omics=='metabolomics') 'log2_supplied_peak_intensity' else 'supplied_log2_median_centered'
  if(!all(is.finite(expression))) stop('Invalid supplied expression scale')
  delta <- sapply(pairs,function(p) expression[,p$biospecimen_id[p$tissue=='tumor']]-expression[,p$biospecimen_id[p$tissue=='adjacent']])
  rownames(delta) <- rownames(expression)
  # Fitting per-patient differences is algebraically paired; no pseudoreplicates.
  fit <- limma::eBayes(limma::lmFit(delta,matrix(1,ncol(delta),1)),trend=FALSE)
  se <- fit$stdev.unscaled[,1]*sqrt(fit$s2.post)
  r <- data.frame(feature_id=rownames(expression),effect=fit$coefficients[,1],se=se,
                  statistic=fit$t[,1],pvalue=fit$p.value[,1],padj=p.adjust(fit$p.value[,1],'BH'),
                  ci_low=fit$coefficients[,1]-qt(.975,fit$df.total)*se,
                  ci_high=fit$coefficients[,1]+qt(.975,fit$df.total)*se,
                  mean_abundance=rowMeans(expression),df=fit$df.total,test_status='tested')
  method <- paste0('limma_',packageVersion('limma'),'_paired_difference_moderated_t')
}
r <- merge(data.frame(feature_id=all_ids,order=seq_along(all_ids)),r,by='feature_id',all.x=TRUE,sort=FALSE)
r <- r[order(r$order),];r$order <- NULL
r$test_status[filter_reason[r$feature_id]!=''] <- filter_reason[r$feature_id][filter_reason[r$feature_id]!='']
r$omics <- job$omics;r$cohort_id <- job$cohort_id;r$contrast <- 'tumor_vs_adjacent';r$n_pairs <- length(pairs)
r$effect_scale <- if(job$omics=='transcriptomics') 'log2_fold_change' else expression_scale
r$method <- method
r$selected <- !is.na(r$padj) & r$padj<=job$alpha & abs(r$effect)>=job$effect_cutoff
r$direction <- ifelse(is.na(r$effect),'not_estimated',ifelse(r$effect>0,'up',ifelse(r$effect<0,'down','zero')))
write.csv(r,file.path(out,'de.csv'),row.names=FALSE,na='')
matrix_file(expression,file.path(out,'expression.csv'))
delta <- sapply(pairs,function(p) expression[,p$biospecimen_id[p$tissue=='tumor']]-expression[,p$biospecimen_id[p$tissue=='adjacent']])
rownames(delta) <- rownames(expression)
matrix_file(delta,file.path(out,'paired_delta.csv'))
write.csv(meta,file.path(out,'sample_metadata.csv'),row.names=FALSE)
write.csv(data.frame(feature_id=all_ids,reason=filter_reason),file.path(out,'filter_audit.csv'),row.names=FALSE)
qc <- data.frame(biospecimen_id=colnames(m),patient_id=as.character(meta$patient_id),tissue=as.character(meta$tissue),
                 median=apply(expression,2,median),detected_features=colSums(m!=0),total_input=colSums(m))
write.csv(qc,file.path(out,'sample_qc.csv'),row.names=FALSE)
vary <- apply(expression,1,sd)>0
pca <- prcomp(t(expression[vary,,drop=FALSE]),center=TRUE,scale.=FALSE,rank.=2)
write.csv(data.frame(biospecimen_id=rownames(pca$x),pca$x[,1:2,drop=FALSE],tissue=meta$tissue,patient_id=meta$patient_id),file.path(out,'pca_source.csv'),row.names=FALSE)
write.csv(data.frame(component=seq_along(pca$sdev),variance_fraction=pca$sdev^2/sum(pca$sdev^2)),file.path(out,'pca_variance.csv'),row.names=FALSE)
jsonlite::write_json(list(status='complete',omics=job$omics,cohort_id=job$cohort_id,n_pairs=length(pairs),
  n_features=nrow(r),n_tested=sum(!is.na(r$padj)),up=sum(r$selected & r$direction=='up'),down=sum(r$selected & r$direction=='down'),
  expression_scale=expression_scale,method=method,contrast='tumor_vs_adjacent',seed=job$seed),
  file.path(out,'statistics.json'),auto_unbox=TRUE,pretty=TRUE)
writeLines(capture.output(sessionInfo()),file.path(out,'sessionInfo.txt'))
