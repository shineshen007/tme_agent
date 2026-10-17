# Exported DESeq2 steps, following the installed DESeqParallel sequence.
# Normalization and dispersion trend/prior are estimated globally, never per chunk.
read_checkpoint <- function(path) {
  checksum <- paste0(path,'.md5')
  if(!file.exists(path)) {
    if(file.exists(checksum)) stop('Checkpoint data missing: ',path)
    return(NULL)
  }
  if(!file.exists(checksum)) {
    # An interrupted commit is preserved and recomputed; a checksum mismatch fails closed.
    archived <- paste0(path,'.incomplete.',format(Sys.time(),'%Y%m%dT%H%M%S'),'.',Sys.getpid())
    if(!file.rename(path,archived)) stop('Cannot preserve incomplete checkpoint: ',path)
    message('Preserved incomplete checkpoint: ',archived)
    return(NULL)
  }
  if(!identical(unname(tools::md5sum(path)),readLines(checksum))) stop('Checkpoint checksum mismatch: ',path)
  readRDS(path)
}

save_checkpoint <- function(value,path) {
  dir.create(dirname(path),recursive=TRUE,showWarnings=FALSE)
  temp <- paste0(path,'.partial.',Sys.getpid())
  saveRDS(value,temp)
  writeLines(unname(tools::md5sum(temp)),paste0(temp,'.md5'))
  if(!file.rename(temp,path) || !file.rename(paste0(temp,'.md5'),paste0(path,'.md5'))) stop('Checkpoint commit failed: ',path)
  invisible(value)
}

fit_dds_checkpointed <- function(dds,out,workers,chunk_size,seed,fit_type) {
  checkpoint_dir <- file.path(out,'checkpoints')
  dir.create(checkpoint_dir,recursive=TRUE,showWarnings=FALSE)
  all_rows <- rownames(dds);all_columns <- colnames(dds)
  chunks <- split(seq_len(nrow(dds)),ceiling(seq_len(nrow(dds))/chunk_size))
  param <- BiocParallel::MulticoreParam(workers=workers,tasks=length(chunks),RNGseed=seed,stop.on.error=TRUE)
  # Same default ratio size factors and reference BLAS as the serial workflow.
  dds <- DESeq2::estimateSizeFactors(dds)
  stage_path <- file.path(checkpoint_dir,'gene_stage.rds')
  gene_stage <- read_checkpoint(stage_path)
  if(is.null(gene_stage)) {
    message('Gene dispersion stage: ',length(chunks),' chunks; ',workers,' workers')
    pieces <- BiocParallel::bplapply(seq_along(chunks),function(i) {
      path <- file.path(checkpoint_dir,'gene_dispersion',sprintf('chunk_%06d.rds',i))
      value <- read_checkpoint(path)
      if(is.null(value)) {
        value <- DESeq2::estimateDispersionsGeneEst(dds[chunks[[i]],],quiet=TRUE,minmu=0.5)
        save_checkpoint(value,path)
        message('Completed gene dispersion chunk ',i,'/',length(chunks))
      }
      stopifnot(identical(rownames(value),all_rows[chunks[[i]]]),identical(colnames(value),all_columns))
      value
    },BPPARAM=param)
    gene_stage <- do.call(BiocGenerics::rbind,pieces)
    S4Vectors::metadata(gene_stage) <- S4Vectors::metadata(gene_stage)[unique(names(S4Vectors::metadata(gene_stage)))]
    save_checkpoint(gene_stage,stage_path)
    rm(pieces);gc()
  }
  stopifnot(identical(rownames(gene_stage),all_rows),identical(colnames(gene_stage),all_columns))
  trend_path <- file.path(checkpoint_dir,'global_dispersion_trend.rds')
  global <- read_checkpoint(trend_path)
  if(is.null(global)) {
    message('Estimating global dispersion trend and prior')
    fitted <- DESeq2::estimateDispersionsFit(gene_stage,fitType=fit_type)
    global <- list(object=fitted,prior_variance=DESeq2::estimateDispersionsPriorVar(fitted))
    save_checkpoint(global,trend_path)
  }
  rm(gene_stage,dds);gc()
  message('MAP dispersion and Wald stage: ',length(chunks),' chunks; ',workers,' workers')
  pieces <- BiocParallel::bplapply(seq_along(chunks),function(i) {
    path <- file.path(checkpoint_dir,'map_wald',sprintf('chunk_%06d.rds',i))
    value <- read_checkpoint(path)
    if(is.null(value)) {
      value <- DESeq2::estimateDispersionsMAP(global$object[chunks[[i]],],dispPriorVar=global$prior_variance,quiet=TRUE)
      value <- DESeq2::nbinomWaldTest(value,betaPrior=FALSE,quiet=TRUE,useT=FALSE,minmu=0.5)
      save_checkpoint(value,path)
      message('Completed MAP/Wald chunk ',i,'/',length(chunks))
    }
    stopifnot(identical(rownames(value),all_rows[chunks[[i]]]),identical(colnames(value),all_columns))
    value
  },BPPARAM=param)
  fitted <- do.call(BiocGenerics::rbind,pieces)
  S4Vectors::metadata(fitted) <- S4Vectors::metadata(fitted)[unique(names(S4Vectors::metadata(fitted)))]
  stopifnot(identical(rownames(fitted),all_rows),identical(colnames(fitted),all_columns))
  fitted
}
