#!/usr/bin/env Rscript
# ============================================================================
# Mirage benchmark plots (ggplot2)
# ============================================================================
# A catalogue of the figures the sweep data supports. Reads the CSVs written by
#   python -m benchmarks.analysis.make_figures ... --outdir benchmarks/analysis
# and writes PDFs to benchmarks/analysis/figures_R/.
#
#   measurements.csv  one row per (run x PROCESS): peak_rss_gb, peak_vmem_gb,
#                     realtime_s, duration_s, cpus, input_gb + every swept param
#                     (varied_axis, target_px, n_channels, n_register_images,
#                      config_id, rep, memory_mode, seg_method, reg_distributed_tiling, ...)
#   resource_stats.csv          per (process,config): n_reps + mean/std/cv
#   classic_vs_distributed_registration.csv   per cell: classic vs distributed RAM/time
#   run_cost.csv                per run: cpu_hours, gpu_hours, wall_clock_s, bottleneck_stage
#   quality.csv                 per run: reg_tre_median_px (feature-error proxy) + n_cells + params
#   segmentation_agreement.csv  pairwise method mask IoU + cell-count ratio
# (the last three are optional — plots that need them are skipped if the file is absent/empty, so a
#  sweep with failed runs still produces every figure it can.)
#
# Run:  Rscript benchmarks/analysis/plots.R  [analysis_dir]   (default: script's dir)
# Deps: tidyverse (ggplot2, dplyr, readr, forcats, tidyr, stringr), scales
# ============================================================================
.need <- c("ggplot2", "dplyr", "readr", "tidyr", "stringr", "forcats", "purrr", "scales")
.missing <- .need[!vapply(.need, requireNamespace, logical(1), quietly = TRUE)]
if (length(.missing))
  stop("Missing R packages: ", paste(.missing, collapse = ", "),
       "\n  install.packages(c(", paste(sprintf('"%s"', .missing), collapse = ", "), "))",
       call. = FALSE)
suppressPackageStartupMessages(lapply(.need, library, character.only = TRUE))

adir  <- if (length(commandArgs(TRUE))) commandArgs(TRUE)[1] else "benchmarks/analysis"
outdir <- file.path(adir, "figures_R"); dir.create(outdir, showWarnings = FALSE, recursive = TRUE)
CAPTION <- "Mirage benchmark sweep · mean over replicate runs · SLURM-isolated per-process resources"
# Save both a vector PDF (for the manuscript) and a 300-dpi PNG (for slides / quick view).
save_fig <- function(p, name, w = 8, h = 5) {
  p <- p + labs(caption = CAPTION)
  ggsave(file.path(outdir, paste0(name, ".pdf")), p, width = w, height = h, device = cairo_pdf)
  ggsave(file.path(outdir, paste0(name, ".png")), p, width = w, height = h, dpi = 300)
}

# Publication theme: generous type, restrained gridlines, bold titles, grey subtitles.
theme_paper <- theme_minimal(base_size = 13) +
  theme(plot.title    = element_text(face = "bold", size = rel(1.05)),
        plot.subtitle = element_text(colour = "grey35", margin = margin(b = 8)),
        plot.caption  = element_text(colour = "grey55", size = rel(.7), hjust = 1),
        plot.title.position = "plot", plot.caption.position = "plot",
        axis.title    = element_text(colour = "grey20"),
        panel.grid.minor = element_blank(),
        panel.grid.major = element_line(linewidth = .3, colour = "grey90"),
        strip.text    = element_text(face = "bold"),
        legend.position = "top", legend.justification = "left",
        plot.margin   = margin(12, 16, 8, 12))
theme_set(theme_paper)
# Okabe-Ito colourblind-safe palette (classic = black/grey, distributed = orange, etc.)
oi <- c("#0072B2","#D55E00","#009E73","#CC79A7","#E69F00","#56B4E9","#F0E442","#000000")

m <- read_csv(file.path(adir, "measurements.csv"), show_col_types = FALSE) %>%
  mutate(proc = str_replace(process, ".*:", ""),                 # leaf process name
         input_gb = as.numeric(input_gb))
size_axes <- c("baseline", "scaling_grid", "registration_grid", "distributed_grid",
               "target_px", "n_channels")

# ── 1. MEMORY SCALING per process (the headline) — peak RSS vs input, log-log ──
# Only the size-varying runs (a fit over the OFAT-knob runs would pile points at one x).
p1 <- m %>% filter(varied_axis %in% size_axes, is.finite(input_gb), input_gb > 0, peak_rss_gb > 0) %>%
  ggplot(aes(input_gb, peak_rss_gb)) +
  geom_point(alpha = .6, colour = oi[1]) +
  geom_smooth(method = "lm", se = FALSE, colour = oi[2], linewidth = .6) +
  facet_wrap(~ proc, scales = "free") +
  scale_x_log10(labels = label_number()) + scale_y_log10() +
  labs(title = "Peak memory scaling per process",
       subtitle = "Each point is a run; line = linear fit. Steeper = RAM grows faster with input size.",
       x = "input (GiB, log10)", y = "peak RSS (GiB, log10)")
save_fig(p1, "01_memory_scaling_per_process", 11, 8)

# ── 2. TIME SCALING per process — realtime vs input ──
p2 <- m %>% filter(varied_axis %in% size_axes, is.finite(input_gb), input_gb > 0, realtime_s > 0) %>%
  ggplot(aes(input_gb, realtime_s)) +
  geom_point(alpha = .6, colour = oi[3]) +
  geom_smooth(method = "lm", se = FALSE, colour = oi[2], linewidth = .6) +
  facet_wrap(~ proc, scales = "free") +
  scale_x_log10() + scale_y_log10(labels = label_number()) +
  labs(title = "Runtime scaling per process",
       subtitle = "Wall-clock per process vs input size (both log scales).",
       x = "input (GiB, log10)", y = "realtime (s, log10)")
save_fig(p2, "02_time_scaling_per_process", 11, 8)

# ── 3. CLASSIC vs DISTRIBUTED registration — the RAM ceiling vs size ──
cvd_path <- file.path(adir, "classic_vs_distributed_registration.csv")
if (file.exists(cvd_path) && nrow(read_csv(cvd_path, show_col_types = FALSE)) > 0) {
  cvd <- read_csv(cvd_path, show_col_types = FALSE)
  long <- cvd %>%
    select(target_px, n_channels,
           classic = reg_peak_rss_gb_classic, distributed = reg_peak_rss_gb_distributed) %>%
    pivot_longer(c(classic, distributed), names_to = "path", values_to = "peak_rss_gb")
  p3 <- ggplot(long, aes(target_px, peak_rss_gb, colour = path)) +
    geom_line(linewidth = .8) + geom_point(size = 2) +
    facet_wrap(~ n_channels, labeller = label_both) +
    scale_x_log10() + scale_colour_manual(values = oi[c(8,2)]) +
    labs(title = "Registration peak RAM: classic vs distributed",
         subtitle = "Classic holds the BioFormats JVM heap (climbs with size); the JVM-free distributed path stays bounded.",
         x = "image size (px, log10)", y = "registration-stage peak RSS (GiB)", colour = NULL)
  save_fig(p3, "03_classic_vs_distributed_ram", 9, 5)

  p3b <- ggplot(cvd, aes(target_px, rss_saving_gb, colour = factor(n_channels))) +
    geom_line(linewidth = .8) + geom_point(size = 2) +
    scale_x_log10() + scale_colour_manual(values = oi, name = "channels") +
    labs(title = "Distributed RAM saving vs classic", x = "image size (px, log)",
         y = "classic − distributed peak RSS (GiB)")
  save_fig(p3b, "03b_distributed_ram_saving", 8, 5)
}

# ── 4. N-IMAGE REGISTRATION — REGISTER cost vs number of slides ──
reg <- m %>% filter(proc == "REGISTER", varied_axis %in% c("registration_grid", "baseline", "scaling_grid"))
if (nrow(reg) > 0) {
  p4 <- reg %>% group_by(target_px, n_channels, n_register_images) %>%
    summarise(peak_rss_gb = mean(peak_rss_gb), realtime_s = mean(realtime_s), .groups = "drop") %>%
    ggplot(aes(n_register_images, peak_rss_gb, colour = factor(target_px))) +
    geom_line() + geom_point(size = 2) +
    facet_wrap(~ n_channels, labeller = label_both) +
    scale_colour_viridis_d(name = "size (px)", option = "C") +
    labs(title = "N-image registration: peak RAM vs slide count",
         subtitle = "Co-registering more slides to one reference; coloured by image size.",
         x = "n_register_images (1 reference + N−1 moving)", y = "peak RSS (GiB)")
  save_fig(p4, "04_nimage_registration_ram", 9, 5)
}

# ── 5. OFAT KNOB EFFECTS — one panel per single-knob axis ──
# For each OFAT axis, plot the most-affected process's realtime vs the knob value.
# Only the true single-knob OFAT axes belong here; memory_mode / skip_micro_registration go to plot 10
# (both paths) and the segmentation tile knobs to plots 9/9b (per method).
knob_targets <- tribble(
  ~axis,                       ~proc,          ~metric,
  "preproc_n_iter",            "PREPROCESS",   "realtime_s",
  "preproc_overlap",           "PREPROCESS",   "realtime_s",
  "preproc_pool_workers",      "PREPROCESS",   "realtime_s",
  "seg_gpu",                   "SEGMENT",      "realtime_s",
  "quantify_compartments",     "QUANTIFY",     "realtime_s",
  "expanded_quantification",   "QUANTIFY",     "realtime_s"
)
knob_df <- pmap_dfr(knob_targets, function(axis, proc, metric) {
  if (!axis %in% names(m)) return(NULL)
  m %>% filter(varied_axis %in% c(axis, "baseline"), proc == !!proc) %>%
    transmute(axis = axis, proc = proc,
              value = as.character(.data[[axis]]), y = .data[[metric]], metric = metric)
})
if (nrow(knob_df) > 0) {
  p5 <- knob_df %>% group_by(axis, proc, metric, value) %>%
    summarise(y = mean(y), .groups = "drop") %>%
    ggplot(aes(fct_inseq(value), y)) +
    geom_col(fill = oi[1], width = .6) +
    facet_wrap(~ paste0(axis, "  (", proc, ": ", metric, ")"), scales = "free") +
    labs(title = "OFAT knob effects (single param varied off baseline)", x = NULL, y = NULL)
  save_fig(p5, "05_ofat_knob_effects", 12, 8)
}

# ── 6. REPLICATE VARIANCE — mean ± sd from the repeats ──
stats_path <- file.path(adir, "resource_stats.csv")
if (file.exists(stats_path)) {
  st <- read_csv(stats_path, show_col_types = FALSE)
  if (nrow(st) > 0 && "peak_rss_gb_mean" %in% names(st)) {
    p6 <- st %>% mutate(proc = str_replace(process, ".*:", "")) %>%
      filter(n_reps > 1) %>%
      ggplot(aes(reorder(proc, peak_rss_gb_mean), peak_rss_gb_mean)) +
      geom_col(fill = oi[6], width = .6) +
      geom_errorbar(aes(ymin = peak_rss_gb_mean - peak_rss_gb_std,
                        ymax = peak_rss_gb_mean + peak_rss_gb_std), width = .3) +
      coord_flip() +
      labs(title = "Peak RSS by process (mean ± sd across repeats)", x = NULL, y = "peak RSS (GiB)")
    save_fig(p6, "06_replicate_variance", 8, 6)
  }
}

# ── 7. STAGE-COST HEATMAP — process x size, fill = peak RSS ──
p7 <- m %>% filter(varied_axis %in% size_axes, n_channels == 2, n_register_images == 2) %>%
  group_by(proc, target_px) %>% summarise(peak_rss_gb = mean(peak_rss_gb), .groups = "drop") %>%
  ggplot(aes(factor(target_px), fct_reorder(proc, peak_rss_gb), fill = peak_rss_gb)) +
  geom_tile(colour = "white") +
  scale_fill_viridis_c(option = "B", trans = "log10", name = "peak RSS\n(GiB)") +
  labs(title = "Where the memory goes",
       subtitle = "Peak RSS by stage \u00d7 image size (log colour). Darker = the memory bottleneck at that size.",
       x = "image size (px)", y = NULL)
save_fig(p7, "07_stage_memory_heatmap", 9, 6)

# ── 8. CHANNEL EFFECT — does 2 vs 4 channels shift the memory scaling? ──
p8 <- m %>% filter(varied_axis %in% size_axes, is.finite(input_gb), input_gb > 0,
                   proc %in% c("REGISTER","PREPROCESS","SEGMENT","QUANTIFY")) %>%
  ggplot(aes(input_gb, peak_rss_gb, colour = factor(n_channels))) +
  geom_point(alpha = .6) + geom_smooth(method = "lm", se = FALSE, linewidth = .6) +
  facet_wrap(~ proc, scales = "free") +
  scale_x_log10() + scale_y_log10() +
  scale_colour_manual(values = oi[c(1,2)], name = "channels") +
  labs(title = "Channel-count effect on memory scaling", x = "input (GiB, log)", y = "peak RSS (GiB, log)")
save_fig(p8, "08_channel_effect", 10, 7)

# ── 9. SEGMENTATION METHODS — each backend with its own parameter sweep ──
seg <- m %>% filter(str_starts(varied_axis, "segmentation_grid"), proc == "SEGMENT")
if (nrow(seg) > 0) {
  p9 <- seg %>%
    ggplot(aes(seg_method, realtime_s, colour = seg_method)) +
    geom_boxplot(outlier.shape = NA, width = .5) +
    geom_jitter(width = .12, alpha = .5, size = 1) +
    scale_colour_manual(values = oi, guide = "none") +
    labs(title = "Segmentation methods compared",
         subtitle = "Box = IQR across each method's own parameter sweep; points = individual configs.",
         x = NULL, y = "SEGMENT realtime (s)")
  save_fig(p9, "09_segmentation_methods", 8, 5)

  # StarDist tile grid effect (its own params)
  sd <- seg %>% filter(seg_method == "stardist")
  if (nrow(sd) > 0) {
    p9b <- sd %>% group_by(seg_n_tiles_x, seg_n_tiles_y) %>%
      summarise(peak_rss_gb = mean(peak_rss_gb), .groups = "drop") %>%
      ggplot(aes(factor(seg_n_tiles_x), factor(seg_n_tiles_y), fill = peak_rss_gb)) +
      geom_tile(colour = "white") + scale_fill_viridis_c(option = "D", name = "peak RSS\n(GiB)") +
      labs(title = "StarDist tiling: peak RSS vs tile grid", x = "seg_n_tiles_x", y = "seg_n_tiles_y")
    save_fig(p9b, "09b_stardist_tile_grid", 6, 5)
  }
}

# ── 10. REGISTRATION PARAMETERS in BOTH paths — memory_mode / skip_micro, classic vs distributed ──
reg_leaves <- c("REGISTER","REG_PREP","REG_TILE","REG_NONRIGID","REG_MICRO_PREP",
                "REG_FINALIZE","REG_FINALIZE_FIELD","REG_FINALIZE_MICRO","REG_WARP_REF")
truthy <- function(x) tolower(as.character(x)) %in% c("true","1","yes")
rp <- m %>% filter(varied_axis == "registration_param_grid", proc %in% reg_leaves)
if (nrow(rp) > 0) {
  p10 <- rp %>%
    group_by(run_id, memory_mode, skip_micro_registration, reg_distributed_tiling) %>%
    summarise(reg_peak_gb = max(peak_rss_gb), .groups = "drop") %>%
    mutate(path = ifelse(truthy(reg_distributed_tiling), "distributed", "classic")) %>%
    group_by(memory_mode, skip_micro_registration, path) %>%
    summarise(reg_peak_gb = mean(reg_peak_gb), .groups = "drop") %>%
    ggplot(aes(fct_relevel(memory_mode, "low", "medium", "high"), reg_peak_gb, fill = path)) +
    geom_col(position = "dodge", width = .7) +
    facet_wrap(~ skip_micro_registration, labeller = label_both) +
    scale_fill_manual(values = oi[c(8, 2)], name = NULL) +
    labs(title = "Registration knobs, measured in both paths",
         subtitle = "memory_mode \u00d7 skip_micro_registration \u2014 classic vs distributed registration.",
         x = "memory_mode", y = "registration-stage peak RSS (GiB)")
  save_fig(p10, "10_registration_params_both_paths", 9, 5)
}

# Helper: read an optional analysis CSV, returning NULL if absent/empty (keeps plots robust to
# failed runs / signals the sweep didn't produce).
read_opt <- function(name) {
  p <- file.path(adir, name)
  if (!file.exists(p)) return(NULL)
  d <- suppressWarnings(read_csv(p, show_col_types = FALSE))
  if (nrow(d) == 0) NULL else d
}
truthy <- function(x) tolower(as.character(x)) %in% c("true", "1", "yes")

# ── 11. REGISTRATION ACCURACY vs COST (the Pareto view) ──
qual <- read_opt("quality.csv"); cost <- read_opt("run_cost.csv")
if (!is.null(qual) && !is.null(cost) && "reg_tre_median_px" %in% names(qual)) {
  ac <- qual %>% select(any_of(c("run_id","varied_axis","memory_mode","skip_micro_registration",
                                 "reg_tre_median_px"))) %>%
    inner_join(cost %>% select(run_id, cpu_hours), by = "run_id") %>%
    filter(is.finite(reg_tre_median_px))
  if (nrow(ac) > 0) {
    p11 <- ac %>%
      ggplot(aes(cpu_hours, reg_tre_median_px)) +
      geom_point(aes(colour = if ("memory_mode" %in% names(ac)) memory_mode else NULL,
                     shape  = if ("skip_micro_registration" %in% names(ac))
                                factor(skip_micro_registration) else NULL), size = 3, alpha = .8) +
      scale_colour_manual(values = oi, name = "memory_mode", na.translate = FALSE) +
      scale_shape_discrete(name = "skip_micro") +
      labs(title = "Registration accuracy vs cost",
           subtitle = "Lower-left is better: less error for fewer CPU-hours. Each point is a config.",
           x = "registration CPU-hours", y = "feature TRE, median (px)")
    save_fig(p11, "11_accuracy_vs_cost", 8, 5)
  }
}

# ── 12. SEGMENTATION METHOD QUALITY — cell count by method + cross-method agreement ──
if (!is.null(qual) && "n_cells" %in% names(qual) && "seg_method" %in% names(qual)) {
  sc <- qual %>% filter(is.finite(n_cells))
  if (nrow(sc) > 0) {
    p12 <- ggplot(sc, aes(seg_method, n_cells, colour = seg_method)) +
      geom_boxplot(outlier.shape = NA, width = .5) + geom_jitter(width = .12, alpha = .5) +
      scale_colour_manual(values = oi, guide = "none") +
      labs(title = "Segmentation: cells detected per method",
           subtitle = "Spread = each method's own parameter sweep. Large gaps = methods disagree on cell count.",
           x = NULL, y = "cells detected (max mask label)")
    save_fig(p12, "12_segmentation_cell_counts", 8, 5)
  }
}
agree <- read_opt("segmentation_agreement.csv")
if (!is.null(agree) && "foreground_iou" %in% names(agree)) {
  p12b <- agree %>% mutate(pair = paste(method_a, "vs", method_b)) %>%
    ggplot(aes(pair, foreground_iou, fill = pair)) +
    geom_col(width = .6) + geom_text(aes(label = sprintf("ratio %.2f", cell_count_ratio)), vjust = -.4, size = 3) +
    scale_fill_manual(values = oi, guide = "none") + ylim(0, 1) +
    labs(title = "Segmentation cross-method agreement",
         subtitle = "Foreground IoU between methods' masks (1 = identical); label = cell-count ratio.",
         x = NULL, y = "foreground IoU")
  save_fig(p12b, "12b_segmentation_agreement", 8, 5)
}

# ── 13. END-TO-END COST — CPU-hours (and wall-clock) vs image size ──
if (!is.null(cost) && "target_px" %in% names(cost)) {
  size_cost <- cost %>% filter(varied_axis %in% size_axes) %>%
    group_by(target_px) %>%
    summarise(cpu_hours = mean(cpu_hours),
              wall_clock_h = mean(wall_clock_s, na.rm = TRUE) / 3600, .groups = "drop")
  if (nrow(size_cost) > 0) {
    p13 <- size_cost %>% pivot_longer(c(cpu_hours, wall_clock_h), names_to = "metric", values_to = "hours") %>%
      filter(is.finite(hours)) %>%
      ggplot(aes(target_px, hours, colour = metric)) +
      geom_line(linewidth = .8) + geom_point(size = 2) +
      scale_x_log10() + scale_colour_manual(values = oi[c(1, 2)],
        labels = c(cpu_hours = "CPU-hours", wall_clock_h = "wall-clock (h)"), name = NULL) +
      labs(title = "End-to-end pipeline cost vs image size",
           subtitle = "Total compute (CPU-hours) and wall-clock per slide.",
           x = "image size (px, log10)", y = "hours")
    save_fig(p13, "13_end_to_end_cost", 8, 5)
  }
}

# ── 14. BOTTLENECK STAGE by image size — which stage dominates wall-clock where ──
if (!is.null(cost) && all(c("bottleneck_stage", "target_px") %in% names(cost))) {
  bn <- cost %>% filter(varied_axis %in% size_axes, !is.na(bottleneck_stage))
  if (nrow(bn) > 0) {
    p14 <- bn %>% count(target_px, bottleneck_stage) %>%
      ggplot(aes(factor(target_px), n, fill = bottleneck_stage)) +
      geom_col(position = "fill") +
      scale_fill_manual(values = oi, name = "bottleneck") +
      scale_y_continuous(labels = percent_format()) +
      labs(title = "Pipeline bottleneck by image size",
           subtitle = "Share of runs whose slowest single process is each stage — the bottleneck shifts with size.",
           x = "image size (px)", y = "share of runs")
    save_fig(p14, "14_bottleneck_by_size", 8, 5)
  }
}

# ── 15. DISTRIBUTED TILED PATH — granularity study (tile size x overlap) ──
# The tiled fan-out is a DIFFERENT algorithm from classic (excluded from the parity comparison), so it
# gets its own figure: registration-stage peak RSS + compute vs tile_wh, coloured by tile_buffer.
if ("varied_axis" %in% names(m) && any(m$varied_axis == "distributed_tiling_grid")) {
  reg_leaves2 <- c("REG_PREP","REG_TILE","REG_NONRIGID","REG_FINALIZE","REG_FINALIZE_FIELD",
                   "REG_FINALIZE_MICRO","REG_WARP_REF","REG_MICRO_PREP")
  tg <- m %>% filter(varied_axis == "distributed_tiling_grid", proc %in% reg_leaves2)
  if (nrow(tg) > 0 && all(c("reg_dist_tile_wh","reg_dist_tile_buffer") %in% names(tg))) {
    p15 <- tg %>%
      group_by(reg_dist_tile_wh, reg_dist_tile_buffer, run_id) %>%
      summarise(reg_peak_gb = max(peak_rss_gb), reg_time_s = sum(realtime_s), .groups = "drop") %>%
      group_by(reg_dist_tile_wh, reg_dist_tile_buffer) %>%
      summarise(reg_peak_gb = mean(reg_peak_gb), reg_time_s = mean(reg_time_s), .groups = "drop") %>%
      ggplot(aes(factor(reg_dist_tile_wh), reg_peak_gb, fill = factor(reg_dist_tile_buffer))) +
      geom_col(position = "dodge", width = .7) +
      scale_fill_manual(values = oi, name = "tile_buffer (px)") +
      labs(title = "Distributed tiled path: RAM vs tile granularity",
           subtitle = "Registration-stage peak RSS by tile size and overlap (the fan-out is a separate algorithm from classic).",
           x = "reg_dist_tile_wh (px)", y = "registration-stage peak RSS (GiB)")
    save_fig(p15, "15_tiled_path_granularity", 8, 5)
  }
}

# ── 16. TILED PATH DRIFT from classic — how far the tiled fan-out moves from classic whole-image ──
drift <- read_opt("registration_drift.csv")
if (!is.null(drift) && "path" %in% names(drift)) {
  td <- drift %>% filter(path == "tiled", is.finite(max_abs_delta))
  if (nrow(td) > 0 && all(c("tile_wh", "tile_buffer") %in% names(td))) {
    p16 <- td %>% group_by(tile_wh, tile_buffer) %>%
      summarise(max_abs_delta = mean(max_abs_delta), pct_pixels_diff = mean(pct_pixels_diff), .groups = "drop") %>%
      ggplot(aes(factor(tile_wh), max_abs_delta, fill = factor(tile_buffer))) +
      geom_col(position = "dodge", width = .7) +
      scale_fill_manual(values = oi, name = "tile_buffer (px)") +
      labs(title = "Tiled path: pixel drift from classic",
           subtitle = "max|Δ| vs the classic slide, by tile size/overlap (0 = identical). Drift, not a failure — tiled is a different algorithm.",
           x = "reg_dist_tile_wh (px)", y = "max |Δ| vs classic (intensity levels)")
    save_fig(p16, "16_tiled_drift_from_classic", 8, 5)
  }
}

# ── 17. REGISTRATION ERROR by path — feature-TRE for classic vs separated vs tiled ──
if (!is.null(qual) && "reg_tre_median_px" %in% names(qual) && "reg_distributed_tiling" %in% names(qual)) {
  ep <- qual %>% filter(is.finite(reg_tre_median_px)) %>%
    mutate(path = case_when(!truthy(reg_distributed_tiling) ~ "classic",
                            "reg_dist_force_tiling" %in% names(.) & truthy(reg_dist_force_tiling) ~ "tiled",
                            TRUE ~ "separated"))
  if (nrow(ep) > 0 && dplyr::n_distinct(ep$path) > 1) {
    p17 <- ggplot(ep, aes(path, reg_tre_median_px, colour = path)) +
      geom_boxplot(outlier.shape = NA, width = .5) + geom_jitter(width = .12, alpha = .6) +
      scale_colour_manual(values = oi[c(8, 1, 2)], guide = "none") +
      labs(title = "Registration error by path (accuracy, not just pixel drift)",
           subtitle = "Feature-based TRE proxy (median px). Separated should match classic; tiled shows any accuracy cost of tiling.",
           x = NULL, y = "feature TRE, median (px)")
    save_fig(p17, "17_registration_error_by_path", 8, 5)
  }
}

message("Wrote figures to ", normalizePath(outdir))
