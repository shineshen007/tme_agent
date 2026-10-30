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
 need(!anyDuplicated(names(x)), 'Duplicate RDS column names')
 need(all(unlist(c$columns)%in%names(x)), 'Missing RDS columns')
 need(all(vapply(x, is.atomic, logical(1))), 'Nested RDS table columns are unsupported')
 if(!isTRUE(c$allow_empty)) need(nrow(x)>0, 'Empty RDS table')
 if(length(c$nonmissing)) for(nm in unlist(c$nonmissing, use.names=FALSE)) {
   missing <- is.na(x[[nm]]) | trimws(tolower(as.character(x[[nm]]))) %in% c('', 'na', 'nan', 'null')
   need(!any(missing), 'Missing required output value')
 }
 if(length(c$finite)) for(nm in unlist(c$finite, use.names=FALSE))
   need(is.numeric(x[[nm]]) && all(is.finite(x[[nm]])), 'Nonfinite output')
 if(length(c$unique_by)) need(!anyDuplicated(x[unlist(c$unique_by, use.names=FALSE)]),
                              'Duplicate output key')
 if(!is.null(r$binding)) {
   need(!is.null(c$sample_key) && c$sample_key %in% names(x), 'Bound RDS table lacks sample_key')
   hashes <- vapply(as.character(x[[c$sample_key]]), digest::digest, '',
                    algo='sha256', serialize=FALSE)
   need(all(hashes %in% names(r$binding)), 'Unexpected output sample')
   need(setequal(hashes, names(r$binding)), 'Output silently dropped samples')
   if(!is.null(c$group_key)) need(c$group_key %in% names(x), 'Output group_key column is missing')
   if(!is.null(c$pair_key)) need(c$pair_key %in% names(x), 'Output pair_key column is missing')
   for(i in seq_len(nrow(x))) {
     row <- r$binding[[hashes[[i]]]]
     if(!is.null(c$group_key))
       need(as.character(x[[c$group_key]][i]) == row[[1]], 'Output group disagrees with frozen metadata')
     if(!is.null(c$pair_key)) {
       pair_hash <- digest::digest(as.character(x[[c$pair_key]][i]), algo='sha256', serialize=FALSE)
       need(!is.null(row[[2]]) && pair_hash == row[[2]], 'Output pairing disagrees with frozen metadata')
     }
   }
 }
} else if(fmt=='rds_list') {
 need(is.list(x), 'Expected RDS list')
 need(all(unlist(c$keys)%in%names(x)), 'Missing RDS list keys')
}
jsonlite::write_json(list(status='passed',dimensions=as.integer(dim(x)),object_class=class(x)),a[2],auto_unbox=TRUE)
