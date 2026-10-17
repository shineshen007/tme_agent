args <- commandArgs(trailingOnly = TRUE)
stopifnot(length(args) == 1L)
root <- normalizePath(args[1], mustWork = TRUE)
target <- file.path(root, 'knowledge', 'resources')
if (dir.exists(target)) stop('Resource directory exists; reuse the frozen resources or select a new project copy.')
dir.create(target, recursive = TRUE)
Sys.setenv(R_USER_CACHE_DIR = file.path(root, '.cache'))
set.seed(2024)
required <- c('AnnotationDbi','org.Hs.eg.db','msigdbr','jsonlite')
for(p in required) if(!requireNamespace(p,quietly=TRUE)) stop(p, ' not installed')
db <- org.Hs.eg.db::org.Hs.eg.db
mapping <- AnnotationDbi::select(db, keys = AnnotationDbi::keys(db, keytype='ENSEMBL'),
                                 keytype='ENSEMBL', columns=c('SYMBOL','ENTREZID'))
write.csv(unique(mapping), file.path(target,'gene_mapping.csv'), row.names=FALSE)
write.csv(AnnotationDbi::metadata(db),file.path(target,'gene_mapping_metadata.csv'),row.names=FALSE)
h <- msigdbr::msigdbr(db_species='HS', species='Homo sapiens', collection='H')
write_gmt <- function(df, path) {
  sets <- split(df$gene_symbol, df$gs_name)
  writeLines(vapply(names(sets),function(n) paste(c(n, 'MSigDB_Hallmark', sort(unique(sets[[n]]))),collapse='\t'), character(1)), path)
}
write_gmt(h,file.path(target,'hallmark.gmt'))
immune <- c('HALLMARK_ALLOGRAFT_REJECTION','HALLMARK_COMPLEMENT','HALLMARK_IL2_STAT5_SIGNALING',
            'HALLMARK_IL6_JAK_STAT3_SIGNALING','HALLMARK_INFLAMMATORY_RESPONSE',
            'HALLMARK_INTERFERON_ALPHA_RESPONSE','HALLMARK_INTERFERON_GAMMA_RESPONSE','HALLMARK_TNFA_SIGNALING_VIA_NFKB')
write_gmt(h[h$gs_name %in% immune,],file.path(target,'immune_programs.gmt'))
jsonlite::write_json(list(status='complete', msigdb_version=unique(h$db_version),
  gene_mapping=paste0('org.Hs.eg.db ',packageVersion('org.Hs.eg.db')),
  mapping_limitation='Database mapping is version-pinned, but original alignment GTF version was not supplied.',
  immune_interpretation='Expression programs; not immune-cell fractions or independent RNA evidence.',
  sources=c('https://www.gsea-msigdb.org/gsea/msigdb','https://bioconductor.org/packages/org.Hs.eg.db'),
  versions=setNames(lapply(required,function(p)as.character(packageVersion(p))),required)),
  file.path(target,'resources.json'),pretty=TRUE,auto_unbox=TRUE)
writeLines(capture.output(sessionInfo()),file.path(target,'sessionInfo.txt'))
