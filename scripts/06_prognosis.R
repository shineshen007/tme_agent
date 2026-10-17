# Deterministic candidate-axis prognosis tool. Called with a frozen JSON job.
# Scope: pre-registered catecholamine-3mt candidate axis, tumor tissue values only,
# triple109 cohort (same-batch-108 sensitivity for metabolomics features).
# Evidence boundary: single-cohort observational associations; not causal, not flux,
# not cell-of-origin; metabolite identity remains ambiguous until targeted validation.
args <- commandArgs(trailingOnly=TRUE)
if(length(args)!=1L) stop('Usage: Rscript --vanilla scripts/06_prognosis.R JOB.json')
if(!requireNamespace('jsonlite',quietly=TRUE)) stop('jsonlite unavailable')
job <- jsonlite::fromJSON(args[1], simplifyVector=FALSE)
if(length(job$r_library_paths)) .libPaths(c(unlist(job$r_library_paths), .libPaths()))
out <- job$output_dir
dir.create(out, recursive=TRUE, showWarnings=FALSE)
set.seed(job$seed)
options(warn=1)
t_start <- Sys.time()
writeLines(capture.output(sessionInfo()), file.path(out,'sessionInfo_start.txt'))
for(p in c('survival','GSVA','BiocParallel','jsonlite')) if(!requireNamespace(p,quietly=TRUE)) stop('Missing package: ',p)
suppressPackageStartupMessages(library(survival))

as_num <- function(x) suppressWarnings(as.numeric(trimws(as.character(x))))

read_matrix <- function(path) {
  if(!file.exists(path)) stop('Missing expression matrix: ', path)
  m <- as.matrix(read.csv(path, row.names=1, check.names=FALSE))
  storage.mode(m) <- 'double'
  if(anyDuplicated(rownames(m))) stop('Duplicate feature identifiers: ', path)
  m
}

load_metadata <- function(path, label) {
  if(!file.exists(path)) stop('Missing sample metadata: ', label)
  d <- read.csv(path, check.names=FALSE, stringsAsFactors=FALSE)
  if(!all(c('patient_id','biospecimen_id','tissue') %in% colnames(d)))
    stop('Sample metadata lacks required columns: ', label)
  d
}

read_gmt <- function(path) {
  if(!file.exists(path)) stop('Missing signature GMT: ', path)
  lines <- readLines(path); lines <- lines[nchar(lines) > 0]
  parts <- strsplit(lines, '\t')
  sets <- lapply(parts, function(x) x[-c(1,2)])
  names(sets) <- vapply(parts, function(x) x[1], character(1))
  sets
}

tumor_samples <- function(meta, patients) {
  d <- meta[meta$tissue=='tumor', c('patient_id','biospecimen_id'), drop=FALSE]
  d <- d[!duplicated(d$patient_id), , drop=FALSE]
  d[d$patient_id %in% patients, , drop=FALSE]
}

# ---------- clinical table (authoritative clinic_full-derived; historical derived fields isolated) ----------
clin <- read.csv(job$clinical, check.names=FALSE, stringsAsFactors=FALSE)
required_clin <- c('patient_id','age','sex','cirrhosis','os_months','os_event','rfs_months','rfs_event',
                   'tumor_purity','tnm_stage','tnm_ordinal')
if(!all(required_clin %in% colnames(clin))) stop('Clinical table lacks required columns')
if(anyDuplicated(clin$patient_id)) stop('Duplicate patient_id in clinical table')
clin$age <- as_num(clin$age)
clin$os_months <- as_num(clin$os_months); clin$os_event <- as_num(clin$os_event)
clin$rfs_months <- as_num(clin$rfs_months); clin$rfs_event <- as_num(clin$rfs_event)
clin$tumor_purity <- as_num(clin$tumor_purity); clin$tnm_ordinal <- as_num(clin$tnm_ordinal)
clin$cirrhosis_num <- as_num(clin$cirrhosis)
sex_lower <- tolower(trimws(clin$sex))
clin$sex_male <- ifelse(sex_lower=='male', 1L, ifelse(sex_lower=='female', 0L, NA_integer_))

# ---------- frozen cohorts ----------
pat <- read.csv(job$patients, check.names=FALSE, stringsAsFactors=FALSE)
if(!all(c('patient_id','in_triple109') %in% colnames(pat))) stop('patients.csv lacks required columns')
triple <- as.character(pat$patient_id[as.logical(pat$in_triple109)])
if(anyDuplicated(triple)) stop('Duplicate patient in triple109 cohort')
sbm <- read.csv(job$same_batch_metadata, check.names=FALSE, stringsAsFactors=FALSE)
if(!'patient_id' %in% colnames(sbm)) stop('same-batch metadata lacks patient_id')
same_batch <- unique(as.character(sbm$patient_id))
cohorts <- list(triple109=triple, same_batch108=same_batch)

# ---------- features (pre-registered panel resolved upstream; validated here) ----------
features <- do.call(rbind, lapply(job$features, function(f) as.data.frame(f, stringsAsFactors=FALSE)))
if(is.null(features) || nrow(features)==0L) stop('Feature table is empty')
for(col in c('feature_id','omics','entity','axis')) if(!col %in% colnames(features))
  stop('Feature table lacks required column: ', col)

# ---------- expression matrices, estimated proportions, frozen signature scores ----------
mats <- list(); metas <- list()
for(om in names(job$expression)) {
  mats[[om]] <- read_matrix(job$expression[[om]])
  metas[[om]] <- load_metadata(job$sample_metadata[[om]], om)
}
missing_specimens <- 0L
long_rows <- list()
for(i in seq_len(nrow(features))) {
  f <- features[i,]
  om <- as.character(f$omics)
  if(is.null(mats[[om]]) || !as.character(f$feature_id) %in% rownames(mats[[om]])) next
  meta <- tumor_samples(metas[[om]], cohorts$triple109)
  if(!all(meta$biospecimen_id %in% colnames(mats[[om]]))) stop('Missing tumor specimen column for ', f$feature_id)
  missing_specimens <- missing_specimens + (length(cohorts$triple109) - nrow(meta))
  long_rows[[length(long_rows)+1L]] <- data.frame(
    patient_id=meta$patient_id, value_type='axis_feature', omics=om,
    entity=as.character(f$entity), feature_id=as.character(f$feature_id), panel='',
    value=as.numeric(mats[[om]][as.character(f$feature_id), meta$biospecimen_id]), stringsAsFactors=FALSE)
}

for(panel in names(job$proportions)) {
  p <- read.csv(job$proportions[[panel]], check.names=FALSE, stringsAsFactors=FALSE)
  if(!all(c('patient_id','tissue') %in% colnames(p))) stop('Proportion table lacks required columns: ', panel)
  p <- p[p$tissue=='tumor', , drop=FALSE]
  p <- p[!duplicated(p$patient_id), , drop=FALSE]
  p <- p[p$patient_id %in% cohorts$triple109, , drop=FALSE]
  for(ct in job$deconvolution_celltypes) {
    if(!ct %in% colnames(p)) stop('Missing deconvolution cell type ', ct, ' in panel ', panel)
    long_rows[[length(long_rows)+1L]] <- data.frame(
      patient_id=p$patient_id, value_type='tcell_proportion', omics='scrna_deconvolution',
      entity=ct, feature_id=ct, panel=panel, value=as.numeric(p[[ct]]), stringsAsFactors=FALSE)
  }
}

signature_status <- 'complete'; signature_used <- character(0)
sig_map <- do.call(rbind, lapply(job$signature_features, function(x) as.data.frame(x, stringsAsFactors=FALSE)))
if(is.null(sig_map)) sig_map <- data.frame(symbol=character(), feature_id=character())
rna <- mats[['transcriptomics']]
sig_ok <- sig_map[sig_map$feature_id %in% rownames(rna), , drop=FALSE]
sig_ok <- sig_ok[!duplicated(sig_ok$symbol), , drop=FALSE]
if(nrow(sig_ok) < job$signature_min_size) {
  signature_status <- 'blocked_insufficient_signal_genes'
} else {
  sets <- read_gmt(job$signature_gmt)
  meta <- tumor_samples(metas[['transcriptomics']], cohorts$triple109)
  em <- rna[sig_ok$feature_id, meta$biospecimen_id, drop=FALSE]
  rownames(em) <- sig_ok$symbol
  sets_ok <- lapply(sets, function(s) intersect(s, rownames(em)))
  sets_ok <- sets_ok[vapply(sets_ok, length, integer(1)) >= job$signature_min_size]
  if(!length(sets_ok)) {
    signature_status <- 'blocked_insufficient_signal_genes'
  } else {
    score <- tryCatch(GSVA::gsva(GSVA::ssgseaParam(em, sets_ok, minSize=job$signature_min_size, normalize=TRUE),
                                 verbose=FALSE, BPPARAM=BiocParallel::SerialParam()),
                      error=function(e) stop('ssGSEA failed: ', conditionMessage(e)))
    for(set_name in rownames(score)) {
      long_rows[[length(long_rows)+1L]] <- data.frame(
        patient_id=meta$patient_id, value_type='tcell_signature', omics='transcriptomics',
        entity=set_name, feature_id=set_name, panel='', value=as.numeric(score[set_name, ]),
        stringsAsFactors=FALSE)
      signature_used <- c(signature_used, set_name)
    }
  }
}

long <- do.call(rbind, long_rows)
write.csv(long, file.path(out,'patient_features.csv'), row.names=FALSE, na='')

# ---------- Cox models (one exposure per model, standardized per analyzed sample) ----------
fit_cox <- function(d, time_col, event_col, model_type) {
  covariates <- if(model_type=='adjusted') c('age','sex_male','cirrhosis_num','tnm_ordinal') else character(0)
  needed <- c(time_col, event_col, 'value', covariates)
  d <- d[stats::complete.cases(d[, needed, drop=FALSE]), , drop=FALSE]
  n <- nrow(d); ev <- sum(d[[event_col]]==1)
  base <- list(status='not_estimable', reason='insufficient_n_or_events', n=n, n_events=as.integer(ev),
               coef=NA_real_, se=NA_real_, hr=NA_real_, ci_low=NA_real_, ci_high=NA_real_,
               pvalue=NA_real_, ph_p=NA_real_)
  if(n < 10L || ev < job$min_events) return(base)
  if(stats::sd(d$value, na.rm=TRUE)==0) {
    base$reason <- 'zero_variance'; return(base)
  }
  d$x <- as.numeric(scale(d$value))
  formula <- stats::as.formula(paste0('survival::Surv(', time_col, ',', event_col, ') ~ ',
                                      paste(c('x', covariates), collapse=' + ')))
  fit <- tryCatch(survival::coxph(formula, data=d, ties='efron'), error=function(e) NULL)
  if(is.null(fit)) { base$reason <- 'coxph_failed'; return(base) }
  co <- summary(fit)$coefficients
  if(is.null(co) || !'x' %in% rownames(co)) { base$reason <- 'exposure_not_estimable'; return(base) }
  co <- co['x',]
  zph <- tryCatch(unname(survival::cox.zph(fit)$table['x','p']), error=function(e) NA_real_)
  list(status='tested', reason='', n=n, n_events=as.integer(ev), coef=unname(co['coef']),
       se=unname(co['se(coef)']), hr=unname(co['exp(coef)']),
       ci_low=exp(unname(co['coef']) - 1.96*unname(co['se(coef)'])),
       ci_high=exp(unname(co['coef']) + 1.96*unname(co['se(coef)'])),
       pvalue=unname(co['Pr(>|z|)']), ph_p=zph)
}

cox_rows <- list()
for(i in seq_len(nrow(features))) {
  f <- features[i,]
  om <- as.character(f$omics)
  cohort_names <- c('triple109', if(om=='metabolomics' && 'same_batch108' %in% names(cohorts)) 'same_batch108')
  for(cohort in cohort_names) {
    d <- long[long$value_type=='axis_feature' & long$omics==om &
              long$feature_id==as.character(f$feature_id) &
              long$patient_id %in% cohorts[[cohort]], c('patient_id','value'), drop=FALSE]
    if(nrow(d)==0L) next
    d <- merge(d, clin, by='patient_id', all.x=FALSE)
    for(outcome in c('os','rfs')) {
      time_col <- paste0(outcome,'_months'); event_col <- paste0(outcome,'_event')
      for(model_type in c('univariable','adjusted')) {
        fit <- fit_cox(d, time_col, event_col, model_type)
        cox_rows[[length(cox_rows)+1L]] <- data.frame(
          cohort=cohort, outcome=outcome, model_type=model_type,
          feature_id=as.character(f$feature_id), omics=om, entity=as.character(f$entity),
          axis=as.character(f$axis), effect_scale='hazard_ratio_per_sd_tumor_value',
          n=fit$n, n_events=fit$n_events, coef=fit$coef, se=fit$se, hr=fit$hr,
          ci_low=fit$ci_low, ci_high=fit$ci_high, pvalue=fit$pvalue, ph_p=fit$ph_p,
          status=fit$status, reason=fit$reason, stringsAsFactors=FALSE)
      }
    }
  }
}
cox_columns <- c('cohort','outcome','model_type','feature_id','omics','entity','axis','effect_scale',
                 'n','n_events','coef','se','hr','ci_low','ci_high','pvalue','ph_p','status','reason')
cox <- if(length(cox_rows)) do.call(rbind, cox_rows) else
  data.frame(matrix(nrow=0, ncol=length(cox_columns), dimnames=list(NULL, cox_columns)))
write.csv(cox, file.path(out,'cox_models.csv'), row.names=FALSE, na='')

clinical_out <- clin[clin$patient_id %in% cohorts$triple109,
                     c('patient_id','age','sex','cirrhosis_num','tnm_stage','tnm_ordinal','tumor_purity',
                       'os_months','os_event','rfs_months','rfs_event'), drop=FALSE]
clinical_out$in_triple109 <- TRUE
clinical_out$in_same_batch108 <- clinical_out$patient_id %in% cohorts$same_batch108
write.csv(clinical_out, file.path(out,'patient_clinical.csv'), row.names=FALSE, na='')

events_of <- function(outcome) as.integer(sum(clinical_out[[paste0(outcome,'_event')]]==1, na.rm=TRUE))
elapsed <- round(as.numeric(difftime(Sys.time(), t_start, units='secs')), 1)
jsonlite::write_json(list(
  status='complete',
  method='candidate_axis_tracking_prespecified_tumor_values_only',
  n_patients_triple109=length(cohorts$triple109),
  n_patients_same_batch108=length(cohorts$same_batch108),
  n_events_os_triple109=events_of('os'),
  n_events_rfs_triple109=events_of('rfs'),
  n_features_per_omics=as.list(table(features$omics)),
  n_cox_rows=nrow(cox), n_cox_tested=sum(cox$status=='tested'),
  n_not_estimable=sum(cox$status!='tested'),
  n_missing_feature_specimens=missing_specimens,
  signature_status=signature_status, signature_sets_used=as.list(signature_used),
  signature_genes_mapped=nrow(sig_ok),
  exposure_scale='per_1_sd_of_tumor_values_included_in_each_model',
  multiple_testing='BH_FDR_within_pre_declared_families_split_by_cohort_outcome_and_model_type',
  r_version=R.version.string, survival_version=as.character(packageVersion('survival')),
  gsva_version=as.character(packageVersion('GSVA')), seed=job$seed, elapsed_seconds=elapsed,
  interpretation='Single-cohort observational candidate-axis association; not flux, causality or cell of origin; deconvolution proportions are atlas-signature estimates'),
  file.path(out,'statistics.json'), auto_unbox=TRUE, pretty=TRUE)
writeLines(capture.output(sessionInfo()), file.path(out,'sessionInfo.txt'))
cat('06_prognosis.R complete in', elapsed, 'seconds\n')
