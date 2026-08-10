#!/usr/bin/env Rscript
# ============================================================================
# Mirage benchmark plots (ggplot2)
# ============================================================================
# A catalogue of the figures the sweep data supports. Reads the CSVs written by
#   python -m benchmarks.analysis.make_figures ... --outdir benchmarks/analysis
# and writes PDFs to benchmarks/analysis/figures_R/.
#
#   measurements.csv  one row per (run x PROCESS): peak_rss_gb, peak_vmem_gb,
#                     realtime_s, duration_s, cpus, input_gb, read_gb, write_gb (I/O volume from
#                     trace rchar/wchar) + every swept param
#                     (varied_axis, target_px, n_channels, n_register_images,
#                      config_id, rep, memory_mode, reg_micro_reg, registration_method, seg_method, ...)
# Scaling figures (01/02/02b/08) use LINEAR axes; the power-law fit is done in log space and drawn
# as a curve (β = the log-log exponent, shown per facet strip). No log-log axes anywhere.
#   resource_stats.csv          per (process,config): n_reps + mean/std/cv
#   (classic_vs_distributed_registration.csv was dropped with the archived distributed path)
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
# I/O volume per process (read+write GiB) — present only if the trace carried rchar/wchar
# (load.py parses them into read_gb/write_gb; older CSVs won't have the columns).
has_io <- all(c("read_gb", "write_gb") %in% names(m))
if (has_io) m <- m %>% mutate(total_io_gb = read_gb + write_gb)

# POWER-LAW fit per process: lm(log10(y) ~ log10(x)). The slope β is the scaling exponent (β=1 linear,
# >1 super-linear, <1 sub-linear) — the paper number. β/R² are surfaced in each facet strip and the
# fit is drawn as a curve on LINEAR axes (no log-log), so `powerlaw` returns a fine x-grid, not just
# the two endpoints (a power law is a straight line only in log-log space; on linear axes it curves).
powerlaw <- function(df, xcol, ycol) {
  parts <- lapply(split(df, df$proc), function(d) {
    if (length(unique(d[[xcol]])) < 2) return(NULL)
    f <- lm(log10(d[[ycol]]) ~ log10(d[[xcol]]))
    b <- unname(coef(f)[2]); a <- unname(coef(f)[1]); r2 <- summary(f)$r.squared
    xr <- range(d[[xcol]])
    xs <- 10 ^ seq(log10(xr[1]), log10(xr[2]), length.out = 80)   # smooth curve for linear axes
    data.frame(proc = d$proc[1], x = xs, y = 10 ^ (a + b * log10(xs)), exponent = b, r2 = r2)
  })
  do.call(rbind, parts)
}
powerlaw_plot <- function(df, ycol, point_col, title, ylab) {
  d <- df %>% filter(varied_axis %in% size_axes, is.finite(input_gb), input_gb > 0, .data[[ycol]] > 0)
  pl <- powerlaw(d, "input_gb", ycol)
  if (is.null(pl) || !nrow(pl)) return(NULL)
  # β/R² go into the facet strip label (declutters the panel: no overlapping in-panel text box).
  strip <- pl %>% group_by(proc) %>%
    summarise(l = sprintf("%s  (β=%.2f, R²=%.2f)", first(proc), first(exponent), first(r2)),
              .groups = "drop")
  lookup <- setNames(strip$l, strip$proc)
  relabel <- function(v) ifelse(is.na(lookup[v]), v, lookup[v])   # procs with no fit keep bare name
  ggplot(d, aes(input_gb, .data[[ycol]])) +
    geom_point(alpha = .6, colour = point_col) +
    geom_line(data = pl, aes(x, y), colour = oi[2], linewidth = .6) +
    facet_wrap(~ proc, scales = "free", labeller = labeller(proc = relabel)) +
    labs(title = title,
         subtitle = "Linear axes; curve = fitted power law. β = log-log slope (1 = linear, >1 super-linear, <1 sub-linear).",
         x = "input (GiB)", y = ylab)
}

# ── 1. MEMORY SCALING per process (the headline) — peak RSS vs input, power law (linear axes) ──
save_fig(powerlaw_plot(m, "peak_rss_gb", oi[1], "Peak memory scaling per process (power law)",
                       "peak RSS (GiB)"), "01_memory_scaling_per_process", 11, 8)

# ── 2. TIME SCALING per process — realtime vs input, power law (linear axes) ──
save_fig(powerlaw_plot(m, "realtime_s", oi[3], "Runtime scaling per process (power law)",
                       "realtime (s)"), "02_time_scaling_per_process", 11, 8)

# ── 2b. I/O VOLUME SCALING per process — bytes moved (read+write) vs input, power law ──
if (has_io && any(is.finite(m$total_io_gb) & m$total_io_gb > 0)) {
  io_fig <- powerlaw_plot(m, "total_io_gb", oi[6],
                          "I/O volume scaling per process (power law)", "read + write (GiB)")
  if (!is.null(io_fig)) save_fig(io_fig, "02b_io_volume_scaling", 11, 8)
}

# ── 3. (removed) CLASSIC vs DISTRIBUTED registration ──
# Plotted classic_vs_distributed_registration.csv, which no longer exists: the distributed/tiled VALIS
# path was archived out of the pipeline (git tag archive/tiled-valis-2026-07-24) and its emitter went
# with it, so the block was permanently inert behind its file.exists() guard.
# The live head-to-head is now VALIS vs STARE (registration_method = valis | tiled), swept by
# sweep.yaml's registration_method_grid and reported by `python -m benchmarks.analysis.make_tables`.

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
# Only the true single-knob OFAT axes belong here; memory_mode / reg_micro_reg go to plot 10
# (both paths) and the segmentation tile knobs to plots 9/9b (per method).
knob_targets <- tribble(
  ~axis,                       ~proc,          ~metric,
  "skip_preprocessing",        "PREPROCESS",   "realtime_s",
  "preproc_skip_nuclear",      "PREPROCESS",   "realtime_s",
  "preproc_tile_size",         "PREPROCESS",   "realtime_s",
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
    facet_wrap(~ paste0(axis, "  (", proc, ")"), scales = "free", ncol = 3) +
    labs(title = "OFAT knob effects (single param varied off baseline)",
         subtitle = "Mean realtime (s) per knob value; each panel is one knob, free y-scale.",
         x = NULL, y = "realtime (s)") +
    theme(axis.text.x = element_text(angle = 30, hjust = 1))
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

# ── 7b. I/O VOLUME by stage — mean bytes read vs written per process (which stage is I/O-heavy) ──
if (has_io) {
  io_stage <- m %>% filter(varied_axis %in% size_axes) %>%
    group_by(proc) %>%
    summarise(read = mean(read_gb, na.rm = TRUE), write = mean(write_gb, na.rm = TRUE),
              .groups = "drop") %>%
    pivot_longer(c(read, write), names_to = "direction", values_to = "gb") %>%
    filter(is.finite(gb))
  if (nrow(io_stage) > 0 && any(io_stage$gb > 0)) {
    p7b <- io_stage %>%
      ggplot(aes(fct_reorder(proc, gb, .fun = sum), gb, fill = direction)) +
      geom_col(width = .6) + coord_flip() +
      scale_fill_manual(values = oi[c(1, 2)], name = NULL) +
      labs(title = "I/O volume by stage",
           subtitle = "Mean bytes read/written per process (trace rchar/wchar) — the I/O bottleneck, stacked read + write.",
           x = NULL, y = "I/O volume (GiB)")
    save_fig(p7b, "07b_stage_io_split", 8, 6)
  }
}

# ── 8. CHANNEL EFFECT — does 2 vs 4 channels shift the memory scaling? ──
p8 <- m %>% filter(varied_axis %in% size_axes, is.finite(input_gb), input_gb > 0,
                   proc %in% c("REGISTER","PREPROCESS","SEGMENT","QUANTIFY")) %>%
  ggplot(aes(input_gb, peak_rss_gb, colour = factor(n_channels))) +
  geom_point(alpha = .6) + geom_smooth(method = "lm", se = FALSE, linewidth = .6, formula = y ~ x) +
  facet_wrap(~ proc, scales = "free") +
  scale_colour_manual(values = oi[c(1,2)], name = "channels") +
  labs(title = "Channel-count effect on memory scaling", x = "input (GiB)", y = "peak RSS (GiB)")
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
  # reg_micro_reg is the micro-registration DEPTH (0=none | 1=micro-rigid | 2=+micro non-rigid). It
  # replaced the boolean skip_micro_registration, which the pipeline never actually read.
  p10 <- rp %>%
    group_by(run_id, memory_mode, reg_micro_reg) %>%
    summarise(reg_peak_gb = max(peak_rss_gb), .groups = "drop") %>%
    group_by(memory_mode, reg_micro_reg) %>%
    summarise(reg_peak_gb = mean(reg_peak_gb), .groups = "drop") %>%
    ggplot(aes(fct_relevel(memory_mode, "low", "medium", "high"), reg_peak_gb,
               fill = factor(reg_micro_reg))) +
    geom_col(position = "dodge", width = .7) +
    scale_fill_manual(values = oi, name = "reg_micro_reg") +
    labs(title = "VALIS registration knobs",
         subtitle = "memory_mode \u00d7 reg_micro_reg (micro-registration depth) at the baseline cell.",
         x = "memory_mode", y = "registration-stage peak RSS (GiB)")
  save_fig(p10, "10_registration_params", 9, 5)
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
  ac <- qual %>% select(any_of(c("run_id","varied_axis","memory_mode","reg_micro_reg",
                                 "reg_tre_median_px"))) %>%
    inner_join(cost %>% select(run_id, cpu_hours), by = "run_id") %>%
    filter(is.finite(reg_tre_median_px))
  if (nrow(ac) > 0) {
    p11 <- ac %>%
      ggplot(aes(cpu_hours, reg_tre_median_px)) +
      geom_point(aes(colour = if ("memory_mode" %in% names(ac)) memory_mode else NULL,
                     shape  = if ("reg_micro_reg" %in% names(ac))
                                factor(reg_micro_reg) else NULL), size = 3, alpha = .8) +
      scale_colour_manual(values = oi, name = "memory_mode", na.translate = FALSE) +
      scale_shape_discrete(name = "reg_micro_reg") +
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
if (!is.null(agree) && "instance_f1" %in% names(agree)) {
  p12b <- agree %>% mutate(pair = paste(method_a, "vs", method_b)) %>%
    ggplot(aes(pair, instance_f1)) +
    geom_col(width = .6, fill = oi[1]) +
    geom_text(aes(label = sprintf("count ratio %.2f", cell_count_ratio)), vjust = -.4, size = 3) +
    ylim(0, 1) +
    labs(title = "Segmentation cross-method agreement (instance F1)",
         subtitle = "IoU-matched per-cell F1 between methods (1 = agree on every cell); label = cell-count ratio.",
         x = NULL, y = "instance F1 (IoU-matched)")
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
      scale_colour_manual(values = oi[c(1, 2)],
        labels = c(cpu_hours = "CPU-hours", wall_clock_h = "wall-clock (h)"), name = NULL) +
      labs(title = "End-to-end pipeline cost vs image size",
           subtitle = "Total compute (CPU-hours) and wall-clock per slide.",
           x = "image size (px)", y = "hours")
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

# ── 15/16. (removed) DISTRIBUTED TILED PATH granularity + drift-from-classic ──
# Both keyed off the archived distributed VALIS path: the distributed_tiling_grid sweep, the
# reg_dist_tile_wh / reg_dist_tile_buffer params, and registration_drift.csv. All three were removed
# with the path itself (git tag archive/tiled-valis-2026-07-24), so these blocks could never fire.
# The live granularity knob is reg_tiled_tile on the STARE backend — see plot 17 and, for the numbers,
# `python -m benchmarks.analysis.make_tables`.

# ── 17. REGISTRATION ERROR by METHOD — feature-TRE for VALIS vs STARE/tiled ──
# Was keyed on reg_distributed_tiling (archived). The live comparison is registration_method, swept
# head-to-head by sweep.yaml's registration_method_grid.
if (!is.null(qual) && "reg_tre_median_px" %in% names(qual) && "registration_method" %in% names(qual)) {
  ep <- qual %>% filter(is.finite(reg_tre_median_px))
  if (nrow(ep) > 0 && dplyr::n_distinct(ep$registration_method) > 1) {
    p17 <- ggplot(ep, aes(registration_method, reg_tre_median_px, colour = registration_method)) +
      geom_boxplot(outlier.shape = NA, width = .5) + geom_jitter(width = .12, alpha = .6) +
      scale_colour_manual(values = oi, guide = "none") +
      labs(title = "Registration error by method",
           subtitle = "Feature-based TRE proxy (median px). VALIS is the baseline anchor; STARE/tiled is the JVM-free backend.",
           x = NULL, y = "feature TRE, median (px)")
    save_fig(p17, "17_registration_error_by_method", 8, 5)
  }
}

message("Wrote figures to ", normalizePath(outdir))
