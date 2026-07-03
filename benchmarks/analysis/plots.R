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
save_fig <- function(p, name, w = 8, h = 5)
  ggsave(file.path(outdir, paste0(name, ".pdf")), p, width = w, height = h, device = cairo_pdf)

theme_set(theme_minimal(base_size = 12) +
          theme(panel.grid.minor = element_blank(),
                strip.text = element_text(face = "bold"),
                legend.position = "top"))
# Okabe-Ito colourblind-safe palette
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
       x = "input (GiB, log)", y = "peak RSS (GiB, log)")
save_fig(p1, "01_memory_scaling_per_process", 11, 8)

# ── 2. TIME SCALING per process — realtime vs input ──
p2 <- m %>% filter(varied_axis %in% size_axes, is.finite(input_gb), input_gb > 0, realtime_s > 0) %>%
  ggplot(aes(input_gb, realtime_s)) +
  geom_point(alpha = .6, colour = oi[3]) +
  geom_smooth(method = "lm", se = FALSE, colour = oi[2], linewidth = .6) +
  facet_wrap(~ proc, scales = "free") +
  scale_x_log10() + scale_y_log10(labels = label_number()) +
  labs(title = "Runtime scaling per process", x = "input (GiB, log)", y = "realtime (s, log)")
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
    labs(title = "Registration peak RAM: classic vs distributed (the RAM win grows with size)",
         x = "image size (px, log)", y = "registration-stage peak RSS (GiB)", colour = NULL)
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
    labs(title = "N-image registration: REGISTER peak RAM vs slide count",
         x = "n_register_images (1 ref + N−1 moving)", y = "peak RSS (GiB)")
  save_fig(p4, "04_nimage_registration_ram", 9, 5)
}

# ── 5. OFAT KNOB EFFECTS — one panel per single-knob axis ──
# For each OFAT axis, plot the most-affected process's realtime vs the knob value.
knob_targets <- tribble(
  ~axis,                       ~proc,          ~metric,
  "memory_mode",               "REGISTER",     "peak_rss_gb",
  "skip_micro_registration",   "REGISTER",     "realtime_s",
  "preproc_n_iter",            "PREPROCESS",   "realtime_s",
  "preproc_overlap",           "PREPROCESS",   "realtime_s",
  "preproc_pool_workers",      "PREPROCESS",   "realtime_s",
  "seg_n_tiles_x",             "SEGMENT",      "peak_rss_gb",
  "seg_n_tiles_y",             "SEGMENT",      "peak_rss_gb",
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
  labs(title = "Where the memory goes: peak RSS by stage x image size",
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

message("Wrote figures to ", normalizePath(outdir))
