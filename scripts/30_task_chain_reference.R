# Independent PLAGE reference/verification. Inputs and produced task directory are explicit.
a <- commandArgs(TRUE)
stopifnot(length(a)==5)
m <- as.matrix(readRDS(a[1])); info <- readRDS(a[2]); sets <- readRDS(a[3])
score <- GSVA::gsva(GSVA::plageParam(exprData=m, geneSets=sets, minSize=1, maxSize=Inf), verbose=FALSE)
long <- utils::read.csv(a[4], check.names=FALSE, stringsAsFactors=FALSE)
# The acceptance caller normalizes field names only; values are never transformed.
stopifnot(all(c('sample','group','pair','gene_set','score') %in% names(long)))
stopifnot(nrow(long)==length(sets)*ncol(m), !anyDuplicated(long[c('sample','gene_set')]))
stopifnot(setequal(long$sample,colnames(m)),setequal(long$gene_set,names(sets)))
idx<-match(long$sample,info$ID)
stopifnot(!anyNA(idx), all(long$group==ifelse(info$Type[idx]%in%c('T','Tumor'),'tumor','normal')),all(as.character(long$pair)==as.character(info$Pair[idx])))
v <- score[cbind(match(long$gene_set,rownames(score)),match(long$sample,colnames(score)))]
# Match within numerical precision; no rescaling or direction change is permitted.
stopifnot(isTRUE(all.equal(unname(v),long$score,tolerance=1e-10)))
result<-list(status='passed',matrix_dim=dim(m),score_dim=dim(score),long_rows=nrow(long),
 max_abs_error=max(abs(v-long$score)),sample_order=colnames(score),GSVA=as.character(packageVersion('GSVA')))
# Sample IDs remain in the local task audit only.
jsonlite::write_json(result,a[5],auto_unbox=TRUE,pretty=TRUE)
