# Deterministic external scRNA atlas context tool. Called with a frozen JSON job.
# Evidence boundary: external pan-cancer atlas; donors differ from the bulk cohort.
# Outputs are context/descriptive plus donor-level tests; never independent validation.
args <- commandArgs(trailingOnly=TRUE)
if(length(args)!=1L) stop('Usage: Rscript --vanilla scripts/05_scrna_celltype.R JOB.json')
if(!requireNamespace('jsonlite',quietly=TRUE)) stop('jsonlite unavailable')
job <- jsonlite::fromJSON(args[1], simplifyVector=TRUE)
if(length(job$r_library_paths)) .libPaths(c(job$r_library_paths,.libPaths()))
out <- job$output_dir
dir.create(out, recursive=TRUE, showWarnings=FALSE)
dir.create(file.path(out,'deconvolution'), recursive=TRUE, showWarnings=FALSE)
set.seed(job$seed)
options(warn=1)
t_start <- Sys.time()
writeLines(capture.output(sessionInfo()), file.path(out,'sessionInfo_start.txt'))
required <- c('Seurat','Matrix','limma','GSVA','presto','nnls','jsonlite','BiocParallel')
for(p in required) if(!requireNamespace(p,quietly=TRUE)) stop('Missing package: ',p)
suppressPackageStartupMessages(library(Seurat))
suppressPackageStartupMessages(library(Matrix))

# ---------- Load external atlas (read-only) and freeze structure ----------
if(!file.exists(job$rds_path)) stop('scRNA object not found: ', job$rds_path)
obj <- readRDS(job$rds_path)
if(!inherits(obj,'Seurat')) stop('scRNA object is not a Seurat object: ', paste(class(obj),collapse=','))
ct_col <- job$celltype_column; smp_col <- job$sample_column; src_col <- job$source_column
md <- obj[[]]
for(cc in c(ct_col,smp_col,src_col)) if(!cc %in% colnames(md)) stop('Missing metadata column: ',cc)
if(!job$assay %in% names(obj@assays)) stop('Missing assay: ',job$assay)
DefaultAssay(obj) <- job$assay
# Drop scale.data layers before JoinLayers: we use counts (pseudobulk) and data (log-normalized)
# only; merging 9 split scale.data layers would materialize a dense ~170 GB matrix for no benefit.
assay_obj <- obj@assays[[job$assay]]
scale_names <- grep('^scale\\.data', names(assay_obj@layers), value=TRUE)
assay_obj@layers <- assay_obj@layers[!names(assay_obj@layers) %in% scale_names]
obj@assays[[job$assay]] <- assay_obj
obj <- JoinLayers(obj, assay=job$assay)
layers <- Layers(obj[[job$assay]])
if(!all(c('counts','data') %in% layers)) stop('Joined assay lacks counts/data layers: ', paste(layers,collapse=','))
cnt <- LayerData(obj, assay=job$assay, layer='counts')
dat <- LayerData(obj, assay=job$assay, layer='data')
stopifnot(identical(dim(cnt),dim(dat)), ncol(cnt)==ncol(obj))
if(!all(cnt@x==floor(cnt@x))) stop('counts layer is not integer; external object scale differs from contract')
if(max(dat)>50) stop('data layer does not look log-normalized; external object scale differs from contract')
ct <- as.character(md[[ct_col]]); smp <- as.character(md[[smp_col]]); src <- as.character(md[[src_col]])
zero_cells <- mean(Matrix::colSums(cnt)==0)
if(zero_cells>0.05) stop('Over 5% of cells have zero total counts; object integrity suspect')
# Consistency audit against provided metadata (recorded, not fatal).
count_audit <- 'nCount_column_absent'
if('nCount_RNA' %in% colnames(md)) {
  rel <- abs(Matrix::colSums(cnt)-md$nCount_RNA)/pmax(md$nCount_RNA,1)
  count_audit <- sprintf('median_rel_dev=%.5f;p95_rel_dev=%.5f', median(rel), unname(quantile(rel,0.95)))
}
src_per_sample <- tapply(src, smp, function(x) length(unique(x)))
if(any(src_per_sample!=1L)) stop('A sample spans multiple sources; sample/source contract violated')

inv <- list(n_cells=ncol(obj), n_features=nrow(obj), n_samples=length(unique(smp)), n_sources=length(unique(src)),
  assay=job$assay, layers_after_join=layers, scale_layers_dropped=scale_names, reductions=names(obj@reductions),
  celltype_counts=as.list(table(ct)), source_counts=as.list(table(src)),
  doublet_counts=if('doublet_class' %in% colnames(md)) as.list(table(as.character(md$doublet_class))) else NULL,
  percent_mt_summary=if('percent.mt' %in% colnames(md)) as.list(summary(md$percent.mt)) else NULL,
  zero_count_cell_fraction=zero_cells, counts_vs_metadata_audit=count_audit,
  qc_policy='descriptive_only_no_refiltering_of_external_object',
  load_seconds=round(as.numeric(difftime(Sys.time(),t_start,units='secs')),1),
  r_version=R.version.string, seurat_version=as.character(packageVersion('Seurat')))
jsonlite::write_json(inv, file.path(out,'inventory.json'), auto_unbox=TRUE, pretty=TRUE, null='null')
write.csv(data.frame(celltype=names(table(ct)), n_cells=as.integer(table(ct))), file.path(out,'celltype_counts.csv'), row.names=FALSE)
write.csv(as.data.frame.matrix(table(sample=smp, celltype=ct)), file.path(out,'sample_celltype_composition.csv'))
write.csv(as.data.frame.matrix(table(celltype=ct, source=src)), file.path(out,'celltype_source_composition.csv'))

# ---------- Sample x celltype pseudobulk (raw counts -> CPM -> log1p) ----------
group <- paste(smp, ct, sep='||')
glevels <- sort(unique(group))
gfac <- factor(group, levels=glevels)
G <- Matrix::sparseMatrix(i=seq_along(gfac), j=as.integer(gfac), x=1, dims=c(length(gfac), length(glevels)))
pb <- as.matrix(cnt %*% G); colnames(pb) <- glevels
g_sample <- as.character(tapply(smp, gfac, function(x) x[1]))
g_ct <- as.character(tapply(ct, gfac, function(x) x[1]))
g_src <- as.character(tapply(src, gfac, function(x) x[1]))
g_ncell <- as.integer(table(gfac))
keep_group <- g_ncell >= job$min_cells_per_group
lib <- colSums(pb); stopifnot(all(lib[keep_group]>0))
pb_cpm <- sweep(pb, 2, pmax(lib,1e-12), '/')*1e6
pb_log <- log1p(pb_cpm)
write.csv(data.frame(group=glevels, sample=g_sample, celltype=g_ct, source=g_src, n_cells=g_ncell,
                     kept=keep_group, library_size=lib), file.path(out,'pseudobulk_groups.csv'), row.names=FALSE)

ct_levels <- sort(unique(ct))
ct_stat <- data.frame(celltype=ct_levels, n_cells=as.integer(table(ct)[ct_levels]),
  n_samples_kept=vapply(ct_levels, function(k) sum(keep_group & g_ct==k), integer(1)), stringsAsFactors=FALSE)
ct_stat$testable <- ct_stat$n_cells>=job$min_cells_per_celltype & ct_stat$n_samples_kept>=job$min_samples_per_celltype
write.csv(ct_stat, file.path(out,'celltype_testability.csv'), row.names=FALSE)
testable <- ct_stat$celltype[ct_stat$testable]

# Per-cell detection and expression sums aggregated to groups (for pooled descriptives).
nz <- dat; nz@x <- rep(1, length(nz@x))
det_g <- as.matrix(nz %*% G); colnames(det_g) <- glevels
expr_g <- as.matrix(dat %*% G); colnames(expr_g) <- glevels
rm(nz, G); invisible(gc())
ct_idx <- split(seq_along(glevels), g_ct)

# ---------- Candidate gene sets (frozen before querying the atlas) ----------
map <- read.csv(job$gene_mapping, stringsAsFactors=FALSE)
dmap <- unique(map[,c('ENSEMBL','SYMBOL')]); dmap <- dmap[!is.na(dmap$SYMBOL) & dmap$SYMBOL!='',]
mult <- table(dmap$ENSEMBL); dmap1 <- dmap[dmap$ENSEMBL %in% names(mult)[mult==1L],]
ens2sym <- setNames(dmap1$SYMBOL, dmap1$ENSEMBL)

cand <- data.frame(gene=job$axis_genes, gene_source='prespecified_or_existing_axis', stringsAsFactors=FALSE)
map_audit <- data.frame(source_file=character(), feature_id=character(), symbol=character(), status=character(), stringsAsFactors=FALSE)
add_de_candidates <- function(path, label, id_type) {
  if(is.null(path) || !file.exists(path)) return(NULL)
  de <- read.csv(path, check.names=FALSE)
  sel <- de[!is.na(de$selected) & as.logical(de$selected), ]
  if(!nrow(sel)) return(data.frame(gene=character(), gene_source=character(), stringsAsFactors=FALSE))
  if(id_type=='ENSG') {
    sym <- unname(ens2sym[as.character(sel$feature_id)])
    st <- ifelse(is.na(sym),'unmapped_or_ambiguous','mapped')
    map_audit <<- rbind(map_audit, data.frame(source_file=label, feature_id=sel$feature_id,
      symbol=ifelse(is.na(sym),'',sym), status=st, stringsAsFactors=FALSE))
    data.frame(gene=sym[!is.na(sym)], gene_source=label, stringsAsFactors=FALSE)
  } else {
    data.frame(gene=sel$feature_id, gene_source=label, stringsAsFactors=FALSE)
  }
}
for(cohort in names(job$rna_de)) {
  piece <- add_de_candidates(job$rna_de[[cohort]], paste0('bulk_rna_de_',cohort), 'ENSG')
  if(!is.null(piece)) cand <- rbind(cand, piece)
}
prot_piece <- add_de_candidates(job$prot_de, 'bulk_protein_de_triple109', 'SYMBOL')
if(!is.null(prot_piece)) cand <- rbind(cand, prot_piece)
cand <- cand[cand$gene %in% rownames(dat) | cand$gene_source=='prespecified_or_existing_axis', ]
gene_sources <- tapply(cand$gene_source, cand$gene, function(x) paste(sort(unique(x)), collapse=';'))
genes <- names(gene_sources)
genes_present <- genes[genes %in% rownames(dat)]
write.csv(map_audit, file.path(out,'gene_symbol_mapping_audit.csv'), row.names=FALSE)
write.csv(data.frame(gene=genes, gene_source=unname(gene_sources[genes]), present_in_atlas=genes %in% rownames(dat)),
          file.path(out,'candidate_genes.csv'), row.names=FALSE)

# ---------- Axis/candidate gene cell-type localization ----------
# Primary test: donor(sample)-level paired Wilcoxon on pseudobulk logCPM, one celltype vs the mean
# of the other celltypes of the same sample. Cells are not independent units; per-cell presto
# statistics are recorded as descriptive support only.
loc <- expand.grid(gene=genes, celltype=ct_levels, stringsAsFactors=FALSE)
loc$gene_source <- unname(gene_sources[loc$gene])
loc$n_cells_celltype <- ct_stat$n_cells[match(loc$celltype, ct_stat$celltype)]
loc$n_samples_tested <- NA_integer_
loc$pct_cells_expressing <- NA_real_; loc$mean_expr_lognorm <- NA_real_; loc$pseudobulk_mean_logcpm <- NA_real_
loc$donor_effect_median_diff <- NA_real_; loc$donor_pvalue <- NA_real_; loc$donor_padj <- NA_real_
loc$cell_level_auc <- NA_real_; loc$cell_level_pct_in <- NA_real_; loc$cell_level_pct_out <- NA_real_
loc$cell_level_diff_pct <- NA_real_; loc$cell_level_pvalue <- NA_real_; loc$cell_level_padj <- NA_real_
loc$status <- 'pending'

for(k in ct_levels) {
  rows <- loc$celltype==k
  gk <- ct_idx[[k]]; gk_keep <- gk[keep_group[gk]]
  cells_k <- sum(g_ncell[gk])
  pct <- 100*rowSums(det_g[genes_present, gk, drop=FALSE])/max(cells_k,1)
  mex <- rowSums(expr_g[genes_present, gk, drop=FALSE])/max(cells_k,1)
  pbc <- if(length(gk_keep)) rowMeans(pb_cpm[genes_present, gk_keep, drop=FALSE]) else rep(NA_real_, length(genes_present))
  mi <- match(loc$gene[rows], genes_present)
  loc$pct_cells_expressing[rows] <- pct[mi]
  loc$mean_expr_lognorm[rows] <- mex[mi]
  loc$pseudobulk_mean_logcpm[rows] <- pbc[mi]
}
loc$status[!loc$gene %in% genes_present] <- 'gene_absent_in_atlas'

for(k in testable) {
  rows <- loc$celltype==k & loc$status=='pending'
  if(!any(rows)) next
  gk <- which(g_ct==k & keep_group); ok <- which(g_ct!=k & keep_group)
  sx <- g_sample[gk]; sy <- g_sample[ok]
  uy <- unique(sy); common <- intersect(sx, uy)
  loc$n_samples_tested[rows] <- length(common)
  if(length(common) < job$min_samples_per_celltype) { loc$status[rows] <- 'insufficient_samples'; next }
  X <- pb_log[genes_present, gk, drop=FALSE]
  syf <- factor(sy, levels=uy)
  Ym <- t(rowsum(t(pb_log[genes_present, ok, drop=FALSE]), syf)/as.vector(table(syf)[uy]))
  colnames(Ym) <- uy
  D <- X[, match(common, sx), drop=FALSE] - Ym[, common, drop=FALSE]
  eff <- apply(D, 1, median)
  pv <- apply(D, 1, function(d) if(stats::sd(d)>0) stats::wilcox.test(d, mu=0, exact=FALSE, correct=TRUE)$p.value else NA_real_)
  gi <- match(loc$gene[rows], genes_present)
  loc$donor_effect_median_diff[rows] <- eff[gi]
  loc$donor_pvalue[rows] <- pv[gi]
  loc$status[rows] <- ifelse(is.na(pv[gi]),'zero_variance_not_testable','tested')
}
loc$status[loc$status=='pending'] <- 'celltype_not_testable'
ok_p <- !is.na(loc$donor_pvalue)
loc$donor_padj[ok_p] <- p.adjust(loc$donor_pvalue[ok_p], 'BH')
loc$family_donor <- 'axis_localization_donor_paired'

if(length(genes_present)) {
  w <- presto::wilcoxauc(dat[genes_present, , drop=FALSE], ct)
  w <- w[w$group %in% ct_levels, ]
  wm <- match(paste(w$feature, w$group), paste(loc$gene, loc$celltype))
  loc$cell_level_auc[wm] <- w$auc; loc$cell_level_pct_in[wm] <- w$pct_in
  loc$cell_level_pct_out[wm] <- w$pct_out; loc$cell_level_diff_pct[wm] <- w$pct_in - w$pct_out
  loc$cell_level_pvalue[wm] <- w$pval
  ok_c <- !is.na(loc$cell_level_pvalue)
  loc$cell_level_padj[ok_c] <- p.adjust(loc$cell_level_pvalue[ok_c], 'BH')
}
loc$family_cell <- 'axis_localization_cell_level'
loc$claim_type <- 'external_context'
loc$cohort_id <- 'external_atlas'
write.csv(loc, file.path(out,'axis_localization.csv'), row.names=FALSE, na='')

# By-source descriptive table for axis genes only (confounding transparency; no per-source tests).
axis_genes_present <- intersect(job$axis_genes, rownames(dat))
if(length(axis_genes_present)) {
  sc_key <- paste(g_src, g_ct, sep='||')
  usc <- sort(unique(sc_key)); sc_idx <- split(seq_along(glevels), sc_key)
  rows_src <- lapply(usc, function(s) {
    ii <- sc_idx[[s]]; parts <- strsplit(s, '\\|\\|', fixed=FALSE)[[1]]
    ncells <- sum(g_ncell[ii])
    data.frame(gene=axis_genes_present, source=parts[1], celltype=parts[2], n_groups=length(ii), n_cells=ncells,
      pct_cells_expressing=100*rowSums(det_g[axis_genes_present, ii, drop=FALSE])/max(ncells,1),
      mean_expr_lognorm=rowSums(expr_g[axis_genes_present, ii, drop=FALSE])/max(ncells,1), stringsAsFactors=FALSE)
  })
  by_source <- do.call(rbind, rows_src)
  by_source <- by_source[order(by_source$gene, by_source$source, by_source$celltype), ]
} else {
  by_source <- data.frame(gene=character(), source=character(), celltype=character(), n_groups=integer(),
    n_cells=integer(), pct_cells_expressing=numeric(), mean_expr_lognorm=numeric())
}
by_source$claim_type <- 'external_context_descriptive'
write.csv(by_source, file.path(out,'axis_localization_by_source.csv'), row.names=FALSE, na='')

# ---------- Bulk DE gene-set cell-type enrichment (descriptive + donor-paired) ----------
sets <- list(); set_cohort <- c()
for(cohort in names(job$rna_de)) {
  p <- job$rna_de[[cohort]]
  if(is.null(p) || !file.exists(p)) next
  de <- read.csv(p, check.names=FALSE)
  sel <- de[!is.na(de$selected) & as.logical(de$selected), ]
  up <- unique(unname(ens2sym[as.character(sel$feature_id[sel$effect>0])])); up <- up[!is.na(up)]
  dn <- unique(unname(ens2sym[as.character(sel$feature_id[sel$effect<0])])); dn <- dn[!is.na(dn)]
  if(length(up)>=job$genesets$min_size) { sets[[paste0('rna_up_',cohort)]] <- up; set_cohort[paste0('rna_up_',cohort)] <- cohort }
  if(length(dn)>=job$genesets$min_size) { sets[[paste0('rna_down_',cohort)]] <- dn; set_cohort[paste0('rna_down_',cohort)] <- cohort }
}
gs <- NULL; gs_status <- if(length(sets)) 'complete' else 'blocked_no_rna_de'
if(length(sets)) {
  score_rows <- list()
  for(k in testable) {
    gk <- which(g_ct==k & keep_group)
    if(length(gk) < job$min_samples_per_celltype) next
    mat <- pb_log[, gk, drop=FALSE]
    sc <- GSVA::gsva(GSVA::ssgseaParam(mat, sets, minSize=job$genesets$min_size, normalize=TRUE),
                     verbose=FALSE, BPPARAM=BiocParallel::SerialParam())
    for(i in seq_len(nrow(sc))) {
      score_rows[[length(score_rows)+1]] <- data.frame(geneset=rownames(sc)[i], cohort=set_cohort[rownames(sc)[i]],
        celltype=k, sample=g_sample[gk], score=sc[i, ], stringsAsFactors=FALSE)
    }
  }
  scores <- if(length(score_rows)) do.call(rbind, score_rows) else NULL
  if(is.null(scores)) {
    gs_status <- 'blocked_no_testable_celltypes'
    write.csv(data.frame(geneset=character(), cohort=character(), celltype=character(), n_samples=integer(),
      mean_score=numeric(), effect_median_diff=numeric(), pvalue=numeric(), padj=numeric(), family=character(),
      status=character(), claim_type=character(), cohort_id=character()), file.path(out,'geneset_scores.csv'), row.names=FALSE)
  } else write.csv(scores, file.path(out,'geneset_scores.csv'), row.names=FALSE, na='')
  gs_rows <- list()
  if(!is.null(scores)) for(gset in names(sets)) {
    for(k in testable) {
      sel_k <- scores$geneset==gset & scores$celltype==k
      if(!any(sel_k)) next
      smp_k <- scores$sample[sel_k]; s_k <- scores$score[sel_k]
      oth <- scores[scores$geneset==gset & scores$celltype!=k, ]
      if(!nrow(oth)) next
      ymean <- tapply(oth$score, oth$sample, mean)
      common <- intersect(smp_k, names(ymean))
      if(length(common) < job$min_samples_per_celltype) {
        gs_rows[[length(gs_rows)+1]] <- data.frame(geneset=gset, cohort=set_cohort[gset], celltype=k,
          n_samples=length(common), mean_score=mean(s_k), effect_median_diff=NA_real_, pvalue=NA_real_,
          status='insufficient_samples', stringsAsFactors=FALSE)
        next
      }
      d <- s_k[match(common, smp_k)] - ymean[common]
      p <- if(stats::sd(d)>0) stats::wilcox.test(d, mu=0, exact=FALSE, correct=TRUE)$p.value else NA_real_
      gs_rows[[length(gs_rows)+1]] <- data.frame(geneset=gset, cohort=set_cohort[gset], celltype=k,
        n_samples=length(common), mean_score=mean(s_k), effect_median_diff=median(d), pvalue=p,
        status=ifelse(is.na(p),'zero_variance_not_testable','tested'), stringsAsFactors=FALSE)
    }
  }
  gs <- if(length(gs_rows)) do.call(rbind, gs_rows) else NULL
  if(is.null(gs)) {
    gs_status <- 'blocked_no_testable_celltypes'
    write.csv(data.frame(geneset=character(), cohort=character(), celltype=character(), n_samples=integer(),
      mean_score=numeric(), effect_median_diff=numeric(), pvalue=numeric(), padj=numeric(), family=character(),
      status=character(), claim_type=character(), cohort_id=character()), file.path(out,'geneset_celltype.csv'), row.names=FALSE)
  } else {
    ok <- !is.na(gs$pvalue)
    gs$padj <- NA_real_; gs$padj[ok] <- p.adjust(gs$pvalue[ok], 'BH')
    gs$family <- 'geneset_celltype'
    gs$claim_type <- 'descriptive_context'
    gs$cohort_id <- 'external_atlas'
    write.csv(gs, file.path(out,'geneset_celltype.csv'), row.names=FALSE, na='')
  }
} else {
  write.csv(data.frame(geneset=character(), cohort=character(), celltype=character(), n_samples=integer(),
    mean_score=numeric(), effect_median_diff=numeric(), pvalue=numeric(), padj=numeric(), family=character(),
    status=character(), claim_type=character(), cohort_id=character()), file.path(out,'geneset_celltype.csv'), row.names=FALSE)
}

# ---------- Deconvolution: marker signatures + NNLS on bulk CPM ----------
wall <- presto::wilcoxauc(dat, ct)
wall <- wall[wall$group %in% ct_levels, ]
wall$diff_pct <- wall$pct_in - wall$pct_out
pick_markers <- function(top_n) {
  elig <- wall[wall$pct_in >= job$marker_min_pct*100 & wall$diff_pct > 0, ]
  elig <- elig[order(elig$group, -elig$auc, elig$feature), ]
  pieces <- lapply(split(elig, elig$group), function(d) head(d, top_n))
  if(!length(pieces)) stop('No eligible marker genes; deconvolution contract failed')
  do.call(rbind, pieces)
}
mk_primary <- pick_markers(job$marker_top_n)
mk_sens <- pick_markers(job$marker_sensitivity_top_n)
write.csv(mk_primary, file.path(out,'deconvolution/signature_genes.csv'), row.names=FALSE)
write.csv(mk_sens, file.path(out,'deconvolution/signature_genes_sensitivity.csv'), row.names=FALSE)

signature_matrix <- function(markers, restrict_source) {
  ug <- sort(unique(markers$feature)); ug <- ug[ug %in% rownames(pb_cpm)]
  cols <- lapply(split(markers$feature, markers$group), unique)
  mats <- list()
  for(k in names(cols)) {
    gk <- which(g_ct==k & keep_group)
    if(!is.null(restrict_source)) gk <- gk[g_src[gk]==restrict_source]
    if(length(gk)==0) next
    m <- rep(0, length(ug)); names(m) <- ug
    g_in <- intersect(cols[[k]], ug)
    m[g_in] <- rowMeans(pb_cpm[g_in, gk, drop=FALSE])
    mats[[k]] <- m
  }
  if(!length(mats)) stop('No signature columns could be built')
  do.call(cbind, mats)
}
sig_pan <- signature_matrix(mk_primary, NULL)
sig_hcc <- signature_matrix(mk_primary, job$deconvolution$matched_reference_sensitivity)
sig_pan50 <- signature_matrix(mk_sens, NULL)
write.csv(data.frame(gene=rownames(sig_pan), sig_pan, check.names=FALSE), file.path(out,'deconvolution/signature_matrix_pancancer.csv'), row.names=FALSE)
write.csv(data.frame(gene=rownames(sig_hcc), sig_hcc, check.names=FALSE), file.path(out,'deconvolution/signature_matrix_hcc_reference.csv'), row.names=FALSE)
write.csv(data.frame(gene=rownames(sig_pan50), sig_pan50, check.names=FALSE), file.path(out,'deconvolution/signature_matrix_pancancer_top50.csv'), row.names=FALSE)

bulk <- read.csv(job$bulk_counts, row.names=1, check.names=FALSE)
reg <- read.csv(job$sample_registry, check.names=FALSE)
reg <- reg[reg$omics=='transcriptomics', ]
reg <- reg[order(reg$patient_id, reg$tissue), ]
stopifnot(setequal(colnames(bulk), reg$biospecimen_id))
bulk <- as.matrix(bulk[, reg$biospecimen_id]); storage.mode(bulk) <- 'double'
stopifnot(all(bulk>=0), all(bulk==floor(bulk)))
cpm <- sweep(bulk, 2, colSums(bulk), '/')*1e6
sym_all <- map$SYMBOL[match(rownames(cpm), map$ENSEMBL)]
sym_all[is.na(sym_all)] <- ''
mapped <- sym_all!=''
bulk_sym <- rowsum(cpm[mapped, , drop=FALSE], sym_all[mapped], reorder=TRUE)
write.csv(data.frame(n_bulk_features=nrow(cpm), n_features_mapped_to_symbol=sum(mapped),
  n_symbols_after_aggregation=nrow(bulk_sym), aggregation='sum_cpm_per_symbol_all_mapped_ensembl'),
  file.path(out,'deconvolution/bulk_symbol_audit.csv'), row.names=FALSE)

deconv_panel <- function(sig, panel_name) {
  markers <- intersect(rownames(sig), rownames(bulk_sym))
  if(length(markers) < 20) stop('Fewer than 20 signature markers overlap the bulk symbol space: ', panel_name)
  dropped <- setdiff(rownames(sig), markers)
  A <- as.matrix(sig[markers, , drop=FALSE])
  keep_ct <- colnames(A)[colSums(A)>0]
  A <- A[, keep_ct, drop=FALSE]
  B <- as.matrix(bulk_sym[markers, , drop=FALSE])
  props <- matrix(NA_real_, ncol(B), ncol(A), dimnames=list(colnames(B), keep_ct))
  for(j in seq_len(ncol(B))) {
    fit <- nnls::nnls(A, B[, j])$x
    s <- sum(fit)
    if(is.finite(s) && s>0) props[j, ] <- fit/s
  }
  pdf <- data.frame(biospecimen_id=reg$biospecimen_id, patient_id=reg$patient_id, tissue=reg$tissue,
                    panel=panel_name, props, check.names=FALSE, stringsAsFactors=FALSE)
  write.csv(pdf, file.path(out, sprintf('deconvolution/cell_proportions_%s.csv', panel_name)), row.names=FALSE, na='')
  list(props=pdf, markers_used=length(markers), markers_dropped=length(dropped), celltypes=keep_ct)
}
paired_test <- function(pdf, panel_name) {
  cts <- setdiff(colnames(pdf), c('biospecimen_id','patient_id','tissue','panel'))
  pats <- sort(unique(pdf$patient_id))
  key <- paste0(pdf$patient_id,'|',pdf$tissue)
  trow <- match(paste0(pats,'|tumor'), key)
  nrow_ <- match(paste0(pats,'|adjacent'), key)
  delta <- t(as.matrix(pdf[trow, cts]) - as.matrix(pdf[nrow_, cts]))
  dimnames(delta) <- list(cts, pats)
  complete <- stats::complete.cases(t(delta))
  delta <- delta[, complete, drop=FALSE]
  fit <- limma::eBayes(limma::lmFit(delta, matrix(1, ncol(delta), 1)), trend=FALSE)
  se <- fit$stdev.unscaled[,1]*sqrt(fit$s2.post)
  r <- data.frame(panel=panel_name, celltype=cts, effect=fit$coefficients[,1], se=se, statistic=fit$t[,1],
    pvalue=fit$p.value[,1], ci_low=fit$coefficients[,1]-qt(.975,fit$df.total)*se,
    ci_high=fit$coefficients[,1]+qt(.975,fit$df.total)*se,
    n_pairs=ncol(delta), df=fit$df.total, contrast='tumor_vs_adjacent',
    method=sprintf('nnls_deconvolution_paired_limma_%s', panel_name),
    claim_type='estimate_not_measured_fraction', stringsAsFactors=FALSE)
  r$padj <- p.adjust(r$pvalue, 'BH')
  r$family <- paste0('deconvolution_paired_', panel_name)
  r
}
pan <- deconv_panel(sig_pan, 'pancancer')
hcc_panel <- deconv_panel(sig_hcc, 'hcc_reference')
pan50 <- deconv_panel(sig_pan50, 'pancancer_top50')
paired <- rbind(paired_test(pan$props, 'pancancer'), paired_test(hcc_panel$props, 'hcc_reference'),
                paired_test(pan50$props, 'pancancer_top50'))
write.csv(paired, file.path(out,'deconvolution/deconvolution_paired.csv'), row.names=FALSE, na='')

# Purity consistency audit (descriptive only).
clin <- read.csv(job$clinical, check.names=FALSE)
tum <- pan$props[pan$props$tissue=='tumor', ]
malignant_col <- intersect('Malignant cell', colnames(tum))
purity_rows <- data.frame(panel=character(), celltype=character(), n=integer(), spearman_rho=numeric(),
  pvalue=numeric(), interpretation=character(), stringsAsFactors=FALSE)
if(length(malignant_col)) {
  m <- match(tum$patient_id, clin$patient_id)
  ok <- !is.na(m) & !is.na(clin$tumor_purity[m]) & is.finite(tum[[malignant_col]])
  if(sum(ok)>=10) {
    ct_cor <- suppressWarnings(stats::cor.test(tum[[malignant_col]][ok], clin$tumor_purity[m][ok], method='spearman'))
    purity_rows <- data.frame(panel='pancancer', celltype=malignant_col, n=sum(ok),
      spearman_rho=unname(ct_cor$estimate), pvalue=ct_cor$p.value,
      interpretation='descriptive_consistency_check_HE_purity_vs_estimated_malignant_fraction', stringsAsFactors=FALSE)
  }
}
write.csv(purity_rows, file.path(out,'deconvolution/purity_audit.csv'), row.names=FALSE, na='')

# ---------- UMAP figure source (subsampled, seeded) ----------
umap_status <- 'skipped_no_umap_reduction'
if('umap' %in% names(obj@reductions)) {
  emb <- Seurat::Embeddings(obj, 'umap')
  n <- nrow(emb); cap <- job$umap_figure_cell_cap
  idx <- if(n>cap) sort(sample.int(n, cap)) else seq_len(n)
  write.csv(data.frame(cell=rownames(emb)[idx], UMAP_1=emb[idx,1], UMAP_2=emb[idx,2],
    celltype=ct[idx], source=src[idx], sample=smp[idx], subsampled=n>cap),
    file.path(out,'umap_source.csv'), row.names=FALSE)
  umap_status <- 'complete'
}

elapsed <- round(as.numeric(difftime(Sys.time(), t_start, units='secs')), 1)
jsonlite::write_json(list(status='complete', n_cells=ncol(obj), n_samples=length(unique(smp)),
  n_sources=length(unique(src)), n_celltypes=length(ct_levels), n_celltypes_testable=length(testable),
  n_candidate_genes=length(genes), n_genes_present=length(genes_present),
  deconvolution_markers_used=list(pancancer=pan$markers_used, hcc_reference=hcc_panel$markers_used, pancancer_top50=pan50$markers_used),
  deconvolution_markers_dropped=list(pancancer=pan$markers_dropped, hcc_reference=hcc_panel$markers_dropped, pancancer_top50=pan50$markers_dropped),
  analyses=list(
    list(name='inventory', status='complete', method='descriptive_no_refiltering'),
    list(name='axis_localization', status='complete', n_rows=nrow(loc),
         method='donor_paired_wilcoxon_on_sample_pseudobulk_logcpm_plus_presto_cell_level_descriptive',
         note='cells_not_independent_units; cell-level stats are descriptive support'),
    list(name='axis_localization_by_source', status='complete', n_rows=nrow(by_source),
         method='descriptive_per_source_pooled_expression', note='axis genes only'),
    list(name='geneset_celltype', status=gs_status, n_rows=if(is.null(gs)) 0L else nrow(gs),
         method='ssgsea_sample_pseudobulk_donor_paired_wilcoxon'),
    list(name='deconvolution', status='complete', n_rows=nrow(paired),
         method='nnls_marker_signature_pancancer_primary_hcc_reference_and_top50_sensitivity',
         note='estimates_not_measured_fractions; malignant/epithelial transfer to HCC bulk uncertain'),
    list(name='umap_figure_source', status=umap_status, method='seeded_subsample_of_provided_embedding')),
  interpretation='External pan-cancer atlas context; donors differ from the bulk cohort; not independent validation',
  seed=job$seed, elapsed_seconds=elapsed),
  file.path(out,'statistics.json'), auto_unbox=TRUE, pretty=TRUE)
writeLines(capture.output(sessionInfo()), file.path(out,'sessionInfo.txt'))
cat('05_scrna_celltype.R complete in', elapsed, 'seconds\n')
