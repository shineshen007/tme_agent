a <- commandArgs(TRUE)
r <- jsonlite::read_json(a[1], simplifyVector=FALSE)
c <- r$contract
x <- readRDS(r$path)
need <- function(ok, message) if(!isTRUE(ok)) stop(message)
fmt <- c$format
if(fmt=='rds_matrix') {
 need(is.matrix(x) && is.numeric(x), 'Expected numeric RDS matrix')
 need(length(dim(x))==2 && all(dim(x)>0), 'Empty RDS matrix')
 need(all(is.finite(x)), 'Nonfinite RDS matrix')
 need(!is.null(rownames(x)) && !is.null(colnames(x)), 'Matrix axes need names')
 need(!anyDuplicated(rownames(x)) && !anyDuplicated(colnames(x)), 'Duplicate matrix axis names')
 if(!is.null(r$sample_hashes)) {
  ids <- if(c$sample_axis=='rows') rownames(x) else colnames(x)
  hashes <- vapply(ids, digest::digest, '', algo='sha256', serialize=FALSE)
  need(setequal(unname(hashes), unlist(r$sample_hashes)), 'RDS samples do not match frozen binding')
 }
} else if(fmt=='rds_table') {
 need(is.data.frame(x), 'Expected RDS data.frame')
 need(all(unlist(c$columns)%in%names(x)), 'Missing RDS columns')
 need(nrow(x)>0, 'Empty RDS table')
} else if(fmt=='rds_list') {
 need(is.list(x), 'Expected RDS list')
 need(all(unlist(c$keys)%in%names(x)), 'Missing RDS list keys')
}
jsonlite::write_json(list(status='passed',dimensions=as.integer(dim(x)),object_class=class(x)),a[2],auto_unbox=TRUE)
