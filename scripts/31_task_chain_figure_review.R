# Presentation-only review: preserve the model task; reserve room above every observed value for n labels.
suppressPackageStartupMessages({library(ggplot2); library(grid)})

a <- commandArgs(TRUE)
stopifnot(length(a) == 2L)
base <- a[1]
output <- a[2]
fig_dir <- file.path(output, "figures"); src_dir <- file.path(fig_dir, "source_data"); rep_dir <- file.path(output, "reports")
for (d in c(fig_dir, src_dir, rep_dir)) dir.create(d, recursive = TRUE, showWarnings = FALSE)

sc <- read.csv(file.path(base, "tables", "plage_scores_per_sample.csv"), stringsAsFactors = FALSE)
mt <- readRDS(file.path(base, "objects", "plage_scores_159.rds"))
req <- c("sample", "group", "pair", "gene_set", "score")
if (!all(req %in% names(sc))) stop("missing columns in plage_scores_per_sample.csv")
if (!setequal(rownames(mt), unique(sc$gene_set))) stop("gene sets differ between scores table and matrix")
if (!setequal(colnames(mt), unique(sc$sample))) stop("samples differ between scores table and matrix")
sc$gene_set <- factor(sc$gene_set, levels = rownames(mt))
sc$group <- factor(sc$group, levels = c("normal", "tumor"))
if (any(is.na(sc$group))) stop("unexpected group labels")
if (any(!is.finite(sc$score))) stop("non-finite PLAGE scores")
sc$pair <- as.character(sc$pair)
sc <- sc[order(sc$gene_set, sc$group, sc$pair, sc$sample), , drop = FALSE]

set.seed(20261014)
sc$x_jitter <- 0
for (g in levels(sc$gene_set)) {
  for (gr in levels(sc$group)) {
    idx <- which(sc$gene_set == g & sc$group == gr)
    if (length(idx) > 1) sc$x_jitter[idx] <- stats::runif(length(idx), -0.16, 0.16)
  }
}
sc$x_plot <- as.numeric(sc$group) + sc$x_jitter

fams <- character(0)
if (requireNamespace("systemfonts", quietly = TRUE)) {
  fams <- tryCatch(unique(systemfonts::system_fonts()$family), error = function(e) character(0))
}
font_used <- if ("Arial" %in% fams) "Arial" else if ("Liberation Sans" %in% fams) "Liberation Sans" else "default sans (Arial and Liberation Sans unavailable)"
font_family <- if (font_used %in% c("Arial", "Liberation Sans")) font_used else ""
writeLines(c("requested_font: Arial", paste0("font_used: ", font_used),
             paste0("fallback_used: ", if (identical(font_used, "Arial")) "no" else "yes"),
             paste0("detection_method: systemfonts families (systemfonts available=", requireNamespace("systemfonts", quietly = TRUE), ", n_families=", length(fams), ")")),
           file.path(rep_dir, "font_used.txt"))

cnt <- unique(sc[, c("gene_set", "group", "sample")])
labs <- aggregate(sample ~ gene_set + group, data = cnt, FUN = length)
names(labs)[names(labs) == "sample"] <- "n"
labs$x <- as.numeric(labs$group)
labs$label <- paste0(as.character(labs$group), "\nn = ", labs$n)

nrm <- sc[sc$group == "normal", c("gene_set", "pair", "x_plot", "score")]
tum <- sc[sc$group == "tumor", c("gene_set", "pair", "x_plot", "score")]
seg <- merge(nrm, tum, by = c("gene_set", "pair"), suffixes = c(".normal", ".tumor"))
if (nrow(seg) < 1) stop("no paired normal/tumor segments found")

theme_set(theme_bw(base_size = 9, base_family = font_family))
p <- ggplot(sc, aes(x = x_plot, y = score, group = group)) +
  geom_boxplot(aes(x = as.numeric(group), group = group), width = 0.5, outlier.shape = NA,
               fill = "grey93", colour = "grey35", linewidth = 0.3) +
  geom_segment(data = seg, aes(x = x_plot.normal, y = score.normal, xend = x_plot.tumor, yend = score.tumor),
               inherit.aes = FALSE, colour = "grey60", linewidth = 0.2, alpha = 0.8) +
  geom_point(aes(x = x_plot), size = 0.65, colour = "#2C6FA6", alpha = 0.75) +
  geom_text(data = labs, aes(x = x, y = Inf, label = label), inherit.aes = FALSE,
            vjust = 1.15, size = 2.4, lineheight = 0.9, colour = "grey20") +
  facet_wrap(~ gene_set, scales = "free_y", ncol = 2, labeller = labeller(gene_set = label_wrap_gen(width = 26))) +
  scale_x_continuous(breaks = c(1, 2), labels = c("normal", "tumor"), limits = c(0.42, 2.58)) +
  scale_y_continuous(expand = expansion(mult = c(0.05, 0.24))) +
  labs(x = NULL, y = "PLAGE score") +
  theme(panel.grid.minor = element_blank(),
        axis.text.x = element_text(size = 8),
        strip.text = element_text(size = 8),
        plot.margin = margin(6, 6, 6, 6))

grDevices::cairo_pdf(file.path(fig_dir, "plage_tumor_vs_normal_8panel.pdf"), width = 7.2, height = 9.6, family = font_family)
print(p)
invisible(dev.off())

src <- sc[, c("gene_set", "sample", "group", "pair", "score", "x_jitter", "x_plot")]
src$gene_set <- as.character(src$gene_set); src$group <- as.character(src$group)
write.csv(src, file.path(src_dir, "plage_tumor_vs_normal_8panel.csv"), row.names = FALSE)
message("panels written: ", length(levels(sc$gene_set)), "; source rows: ", nrow(src), "; font: ", font_used)
