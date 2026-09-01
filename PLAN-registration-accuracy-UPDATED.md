# Registration accuracy page + benchmark reconciliation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Reconciled 2026-07-24** to mirage `bench/reconcile-main`: VALIS rTRE is now auto-emitted as
> `registration_valis_rtre.csv` (was hand-concatenated `valis_summary.csv`); the distributed/tiled
> registration path was removed from mirage, so the classic-vs-distributed figures/columns are gone;
> the feature-distance JSON is no longer emitted by default. See the schema contract below.

**Goal:** Build a landmark-free registration-accuracy analysis page from mirage's in-pipeline signals (VALIS feature rTRE + segmentation-overlap Dice/displacement, with the feature-distance JSON as an optional legacy extra), and reconcile the stale accuracy figures in the existing benchmark page to mirage's current schema.

**Architecture:** A new `code/registration_accuracy_plots.R` exposes a pure `build_reg_figs(dir)` that reads mirage `paper_data` CSVs from a directory and returns a named list of house-styled ggplots, skipping any figure whose input is absent (data-optional). `analysis/registration_accuracy.Rmd` is a thin twin of `benchmarks.Rmd` that sources the helper and renders each figure under a derivation note. Separately, `code/benchmark_plots.R` fig 11 is repointed from the dead `quality.csv`/`reg_tre_median_px` columns to `param_matrix.csv`; fig 17 is repurposed from the retired classic-vs-distributed "error by path" into a VALIS-vs-segmentation **agreement** view; fig 12 is repointed to `param_matrix.csv`/`runs_master.csv`.

**Tech Stack:** R 4.3.2, ggplot2, dplyr/tidyr/readr, workflowr, testthat (edition 3), renv. House style via `code/plot_theme.R`.

## Global Constraints

- **2-space indentation, spaces not tabs** (project `.Rproj`).
- **Paths via `here::here()`** — never absolute or wd-relative.
- **Never call `theme_classic()`/`theme_bw()`/`theme_minimal()`** in analysis code — the house theme is set globally by sourcing `code/plot_theme.R`; use bare `theme(...)` for tweaks. Use house palettes only: `oi`, `oi_ext`, `hotcold_cols()`, `scale_*_div/seq/ordinal`.
- **Nothing fabricated / data-optional:** every figure skips silently when its CSV or required columns are absent. No figure renders without real data in `data/benchmark/`.
- **`data/` is gitignored** — never commit CSVs or rendered figures/data. Synthetic fixtures used for tests live only under `tests/` (tiny, schema-shaped) or a tempdir.
- **Gitmoji commit prefixes** (`:sparkles:`, `:bug:`, `:memo:`, `:white_check_mark:`, `:recycle:`) at the start of every commit subject. End commit messages with the repo's Co-Authored-By / Claude-Session trailer.
- **Run R locally bypassing renv autoload** with `R_PROFILE_USER=/dev/null Rscript ...` so code executes rather than only parsing.
- **Out of scope:** external ANHIR/ACROBAT landmark validation; any plot-style overhaul.

Mirage schemas (the contract these tasks build against). **Source of truth:** mirage
`benchmarks/analysis/make_tables.py` emits the whole `paper_data/` table set — the R side just
reads it, no manual concatenation.
- `registration_valis_rtre.csv`: VALIS's own feature-based registration error, **auto-emitted** by
  `make_tables.py` (`quality.harvest_valis_rtre`) — one row per (run, slide). Columns: `run_id,
  summary_csv,` then VALIS's columns **verbatim** (e.g. `img_name`/`name`, `original_rTRE`,
  `rigid_rTRE`, `non_rigid_rTRE`, `n_matches`, and/or the `*_D` raw-distance variants — whatever this
  VALIS build wrote). rTRE is **relative** (fraction of image diagonal), unitless. Replaces the old
  hand-concatenated `valis_summary.csv`; the underlying per-slide CSVs are the same ones the QC
  report renders as "Registration Accuracy (Valis rTRE)". **Verify the exact column names against a
  real file** — the §1 builder detects them, but the id/stage columns must exist.
- `registration_accuracy.csv`: `run_id, patient_id, moving, stage {native,rigid,non_rigid,micro}, n_pairs, pair_fraction, iou_mean, iou_p50, frac_iou_ge_0.5, dice_matched, displacement_px_p50, displacement_px_p90, displacement_um_p50, displacement_um_p90, delta_dice_vs_rigid, delta_disp_um_p50_vs_rigid, delta_disp_px_p50_vs_rigid`. (Matches mirage `quality.harvest_registration_qc` exactly.)
- `param_matrix.csv`: one wide row per run — cost + per-stage RAM/time, `reg_dice_matched, reg_displacement_um_p50, reg_delta_disp_um_p50_vs_rigid, reg_delta_dice_vs_rigid, reg_pair_fraction,` the VALIS-reported medians `valis_non_rigid_D` / `valis_*` (per-run median of each numeric VALIS column — `valis_non_rigid_rTRE` too if VALIS emits rTRE), `seg_quality_score, n_cells` (+ `cpu_hours, gpu_hours,` per-stage `<STAGE>_peak_ram_gb`/`<STAGE>_wall_s`). **No `reg_distributed_tiling` / `reg_dist_*` columns** — the distributed/tiled registration path was removed from mirage (archived `archive/tiled-valis-2026-07-24`).
- `feature_dist/*.json` (**optional / legacy**): `moving_image`, `improvement.distance_reduction_percent`, `before_registration.feature_distances.mean`, `after_registration.feature_distances.mean`. Mirage's benchmark **no longer emits these by default** — the sweep baseline dropped `enable_feature_error` in favour of `reg_qc=2` + VALIS rTRE. `estimate_feature_distances.py` still exists, so they appear only if a run explicitly sets `enable_feature_error`. Any figure keyed on them stays data-optional and simply skips.

## File Structure

- **Create** `code/registration_accuracy_plots.R` — `build_reg_figs(dir)` + module `reg_figs`. One responsibility: turn mirage registration CSVs into the named ggplot list.
- **Create** `analysis/registration_accuracy.Rmd` — the page (replaces the 15-line stub).
- **Create** `tests/testthat/test-registration-accuracy-plots.R` — smoke tests driving `build_reg_figs()` from synthetic CSVs.
- **Modify** `code/benchmark_plots.R` — fig 11 repointed; fig 17 repurposed to agreement; fig 12 repointed; 12b verified.
- **Modify** `analysis/benchmarks.Rmd` — fig_notes 11/17, optional-CSV list, caveat block, Notes bullet, cross-link.
- **Modify** `analysis/index.Rmd` — enrich the registration-accuracy bullet (it is not marked in-progress; just ensure the description matches the built page).

---

### Task 1: `build_reg_figs()` skeleton + §1 VALIS rTRE slopegraph

**Files:**
- Create: `code/registration_accuracy_plots.R`
- Test: `tests/testthat/test-registration-accuracy-plots.R`

**Interfaces:**
- Produces: `build_reg_figs(dir = here::here("data","benchmark")) -> named list of ggplot`; module-level `reg_figs`. Figure keys are prefixed with an order number (`01_...`, `02_...`) exactly like `benchmark_plots.R`.
- Consumes: `code/plot_theme.R` (`oi`, `oi_ext`, `theme_paper` via global `theme_set`), and `registration_valis_rtre.csv` (auto-emitted by mirage `make_tables.py`).

- [ ] **Step 1: Write the failing test**

Create `tests/testthat/test-registration-accuracy-plots.R`:

```r
# Smoke tests for the registration-accuracy figure builder. Each writes a tiny
# schema-shaped CSV to a tempdir and checks the right ggplot is produced — no
# off-repo sweep data required.
source(here::here("code", "registration_accuracy_plots.R"))

tmp_data <- function() {
  d <- file.path(tempdir(), paste0("regfig-", as.integer(Sys.time()), "-", sample(1e6, 1)))
  dir.create(d, recursive = TRUE, showWarnings = FALSE)
  d
}

test_that("empty dir yields an empty figure list without error", {
  expect_length(build_reg_figs(tmp_data()), 0)
})

test_that("registration_valis_rtre.csv builds the rTRE slopegraph and n_matches panel", {
  d <- tmp_data()
  # columns as mirage make_tables.py emits them: run_id + summary_csv + VALIS's own columns
  readr::write_csv(tibble::tibble(
    run_id         = "run0000",
    summary_csv    = "P001_summary.csv",
    img_name       = c("P001_mov1", "P001_mov2"),
    original_rTRE  = c(50.2, 40.0),
    rigid_rTRE     = c(10.1, 9.0),
    non_rigid_rTRE = c(5.1, 4.0),
    n_matches      = c(100, 80)
  ), file.path(d, "registration_valis_rtre.csv"))
  figs <- build_reg_figs(d)
  expect_true("01_valis_rtre_by_stage" %in% names(figs))
  expect_true("01b_valis_n_matches"   %in% names(figs))
  expect_s3_class(figs[["01_valis_rtre_by_stage"]], "ggplot")
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `R_PROFILE_USER=/dev/null Rscript -e 'testthat::test_dir("tests/testthat", filter = "registration-accuracy-plots")'`
Expected: FAIL — cannot open file `code/registration_accuracy_plots.R` (does not exist yet).

- [ ] **Step 3: Write minimal implementation**

Create `code/registration_accuracy_plots.R`:

```r
# =============================================================================
# registration_accuracy_plots.R — landmark-free, in-pipeline registration
# accuracy figures for analysis/registration_accuracy.Rmd. Twin of
# benchmark_plots.R: reads mirage paper_data CSVs from a directory (default
# data/benchmark/) and returns a named list of house-styled ggplots, skipping
# any figure whose CSV/columns are absent. Drop the sweep outputs in and re-knit.
# =============================================================================
.need <- c("ggplot2", "dplyr", "readr", "tidyr", "stringr", "tibble")
.missing <- .need[!vapply(.need, requireNamespace, logical(1), quietly = TRUE)]
if (length(.missing))
  stop("Missing R packages: ", paste(.missing, collapse = ", "), call. = FALSE)
suppressPackageStartupMessages(lapply(.need, library, character.only = TRUE))

source(here::here("code", "plot_theme.R"))   # house theme + oi/oi_ext palettes

REG_CAPTION          <- "Mirage registration QC · landmark-free · per moving slide"
STAGE_LEVELS_RTRE    <- c("original", "rigid", "non_rigid")            # VALIS rTRE stages
STAGE_LEVELS_OVERLAP <- c("native", "rigid", "non_rigid", "micro")     # warp_seg_qc stages

# Return NULL for a missing/empty CSV so a figure guarded on it is skipped.
.reg_read_opt <- function(dir, name) {
  p <- file.path(dir, name)
  if (!file.exists(p)) return(NULL)
  d <- suppressWarnings(readr::read_csv(p, show_col_types = FALSE))
  if (nrow(d) == 0) NULL else d
}

build_reg_figs <- function(dir = here::here("data", "benchmark")) {
  figs <- list()

  # -- §1 VALIS self-reported feature rTRE, per moving slide, across stages -----
  # mirage auto-emits registration_valis_rtre.csv (make_tables.py). Columns are VALIS's
  # verbatim + run_id/summary_csv, so detect the id column and the stage columns rather than
  # hard-coding them: prefer the relative rTRE columns, fall back to the raw distance (_D) ones.
  vs <- .reg_read_opt(dir, "registration_valis_rtre.csv")
  if (!is.null(vs)) {
    id_col <- intersect(c("img_name", "name", "filename", "summary_csv"), names(vs))[1]
    rtre_cols <- grep("_rTRE$", names(vs), value = TRUE)
    metric_lab <- "VALIS rTRE (relative)"
    if (!length(rtre_cols)) {
      rtre_cols <- grep("_D$", names(vs), value = TRUE)
      metric_lab <- "VALIS matched-feature distance (D)"
    }
    rtre_cols <- rtre_cols[sub("_(rTRE|D)$", "", rtre_cols) %in% STAGE_LEVELS_RTRE]
    if (!is.na(id_col) && length(rtre_cols) >= 2) {
      long <- vs %>%
        dplyr::select(dplyr::all_of(c(id_col, rtre_cols))) %>%
        tidyr::pivot_longer(dplyr::all_of(rtre_cols),
                            names_to = "stage", values_to = "rTRE") %>%
        dplyr::mutate(stage = factor(sub("_(rTRE|D)$", "", stage), levels = STAGE_LEVELS_RTRE)) %>%
        dplyr::filter(is.finite(rTRE))
      if (nrow(long)) {
        n_slide <- dplyr::n_distinct(long[[id_col]])
        figs[["01_valis_rtre_by_stage"]] <-
          ggplot(long, aes(stage, rTRE, group = .data[[id_col]], colour = .data[[id_col]])) +
          geom_line(alpha = .8) + geom_point() +
          scale_colour_manual(values = rep_len(oi_ext, n_slide), guide = "none") +
          labs(title = "VALIS registration error by stage",
               subtitle = "Self-reported feature error per moving slide; lower = better.",
               x = NULL, y = metric_lab, caption = REG_CAPTION)
      }
    }
    if ("n_matches" %in% names(vs) && any(is.finite(vs$n_matches)) && !is.na(id_col)) {
      figs[["01b_valis_n_matches"]] <-
        ggplot(vs, aes(stats::reorder(.data[[id_col]], n_matches), n_matches)) +
        geom_col(fill = oi[1], width = .7) + coord_flip() +
        labs(title = "Feature matches behind each rTRE estimate",
             subtitle = "Correspondences VALIS used per slide; few matches = low-confidence estimate.",
             x = NULL, y = "feature matches (n)", caption = REG_CAPTION)
    }
  }

  figs
}

# Module-level list for the Rmd (harmless on an empty data dir: returns list()).
reg_figs <- build_reg_figs()
message("registration_accuracy_plots.R: built ", length(reg_figs), " figure(s)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `R_PROFILE_USER=/dev/null Rscript -e 'testthat::test_dir("tests/testthat", filter = "registration-accuracy-plots")'`
Expected: PASS (3 tests). If packages are missing under `/dev/null`, rerun without it: `Rscript -e 'testthat::test_dir("tests/testthat", filter = "registration-accuracy-plots")'`.

- [ ] **Step 5: Commit**

```bash
git add code/registration_accuracy_plots.R tests/testthat/test-registration-accuracy-plots.R
git commit -m ":sparkles: add registration-accuracy figure builder (§1 VALIS rTRE)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0149ze4SMDcvtmk817juvxXP"
```

---

### Task 2: §2 segmentation-overlap accuracy (Dice + displacement) figures

**Files:**
- Modify: `code/registration_accuracy_plots.R` (add to `build_reg_figs`, before `figs` is returned)
- Test: `tests/testthat/test-registration-accuracy-plots.R` (add a case)

**Interfaces:**
- Produces figure keys `02_overlap_dice_by_stage`, `02b_displacement_um_by_stage`.
- Consumes: `registration_accuracy.csv` columns `stage`, `dice_matched`, `displacement_um_p50`, `displacement_um_p90`, and `moving` (optional, for per-slide lines). (Schema matches mirage exactly — no change from the original plan.)

- [ ] **Step 1: Write the failing test**

Append to `tests/testthat/test-registration-accuracy-plots.R`:

```r
test_that("registration_accuracy.csv builds the overlap Dice and displacement figures", {
  d <- tmp_data()
  readr::write_csv(tibble::tibble(
    run_id             = "r1",
    moving             = rep(c("P001_mov1", "P001_mov2"), each = 4),
    stage              = rep(c("native", "rigid", "non_rigid", "micro"), 2),
    dice_matched       = c(.10, .55, .72, .74, .12, .50, .70, .71),
    displacement_um_p50 = c(9.0, 3.1, 1.3, 1.2, 8.5, 3.4, 1.5, 1.4),
    displacement_um_p90 = c(18.0, 6.2, 2.6, 2.4, 17.0, 6.8, 3.0, 2.8)
  ), file.path(d, "registration_accuracy.csv"))
  figs <- build_reg_figs(d)
  expect_true(all(c("02_overlap_dice_by_stage", "02b_displacement_um_by_stage") %in% names(figs)))
  expect_s3_class(figs[["02b_displacement_um_by_stage"]], "ggplot")
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `R_PROFILE_USER=/dev/null Rscript -e 'testthat::test_dir("tests/testthat", filter = "registration-accuracy-plots")'`
Expected: FAIL — `02_overlap_dice_by_stage` not in names (section not implemented).

- [ ] **Step 3: Write minimal implementation**

In `code/registration_accuracy_plots.R`, insert this block inside `build_reg_figs`, immediately before the final `figs` return:

```r
  # -- §2 independent overlap accuracy (DAPI-nucleus Dice + centroid residual) --
  ra <- .reg_read_opt(dir, "registration_accuracy.csv")
  if (!is.null(ra) && all(c("stage", "dice_matched") %in% names(ra))) {
    ra <- ra %>%
      dplyr::mutate(stage = factor(stage, levels = STAGE_LEVELS_OVERLAP)) %>%
      dplyr::filter(!is.na(stage))
    has_slide <- "moving" %in% names(ra)
    dice_p <- ggplot(ra, aes(stage, dice_matched)) +
      geom_boxplot(outlier.shape = NA, width = .5)
    if (has_slide)
      dice_p <- dice_p + geom_line(aes(group = moving), alpha = .25)
    figs[["02_overlap_dice_by_stage"]] <- dice_p +
      geom_jitter(width = .10, alpha = .5) +
      labs(title = "Nucleus-overlap Dice by registration stage",
           subtitle = "Independent check via DAPI segmentation overlap (not VALIS features); higher = better.",
           x = NULL, y = "matched-nucleus Dice", caption = REG_CAPTION)

    if (all(c("displacement_um_p50", "displacement_um_p90") %in% names(ra))) {
      disp_long <- ra %>%
        tidyr::pivot_longer(c(displacement_um_p50, displacement_um_p90),
                            names_to = "pct", values_to = "um") %>%
        dplyr::mutate(pct = dplyr::recode(pct,
          displacement_um_p50 = "median", displacement_um_p90 = "90th pct")) %>%
        dplyr::filter(is.finite(um))
      figs[["02b_displacement_um_by_stage"]] <-
        ggplot(disp_long, aes(stage, um, colour = pct)) +
        geom_boxplot(outlier.shape = NA, width = .5, position = position_dodge(.6)) +
        scale_colour_manual(values = oi[c(1, 2)], name = NULL) +
        labs(title = "Centroid residual displacement by stage",
             subtitle = "Matched-nucleus centroid distance in physical units; lower = tighter alignment.",
             x = NULL, y = "displacement (µm)", caption = REG_CAPTION)
    }
  }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `R_PROFILE_USER=/dev/null Rscript -e 'testthat::test_dir("tests/testthat", filter = "registration-accuracy-plots")'`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add code/registration_accuracy_plots.R tests/testthat/test-registration-accuracy-plots.R
git commit -m ":sparkles: add §2 overlap Dice + displacement registration figures

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0149ze4SMDcvtmk817juvxXP"
```

---

### Task 3: §3 feature-distance improvement (optional/legacy) + §4 accuracy-vs-cost + §5 VALIS-vs-overlap agreement

**Files:**
- Modify: `code/registration_accuracy_plots.R` (add three blocks before `figs` return)
- Test: `tests/testthat/test-registration-accuracy-plots.R` (add cases)

**Interfaces:**
- Produces figure keys `03_feature_distance_reduction` (optional/legacy), `04_accuracy_vs_cost`, `05_valis_vs_overlap_agreement`.
- Consumes: `feature_dist/*.json` (optional/legacy — mirage no longer emits by default; needs `jsonlite`; skipped if absent), `param_matrix.csv` columns `reg_displacement_um_p50`, `cpu_hours`, and for §5 a `valis_non_rigid_*` column + `reg_dice_matched`.

- [ ] **Step 1: Write the failing test**

Append to `tests/testthat/test-registration-accuracy-plots.R`:

```r
test_that("param_matrix.csv builds the accuracy-vs-cost Pareto figure", {
  d <- tmp_data()
  readr::write_csv(tibble::tibble(
    run_id                = c("r1", "r2", "r3"),
    cpu_hours             = c(1.2, 2.5, 4.1),
    reg_displacement_um_p50 = c(2.1, 1.4, 1.3)
  ), file.path(d, "param_matrix.csv"))
  figs <- build_reg_figs(d)
  expect_true("04_accuracy_vs_cost" %in% names(figs))
  expect_s3_class(figs[["04_accuracy_vs_cost"]], "ggplot")
})

test_that("param_matrix.csv builds the VALIS-vs-overlap agreement figure", {
  d <- tmp_data()
  readr::write_csv(tibble::tibble(
    run_id            = c("r1", "r2", "r3"),
    reg_dice_matched  = c(0.72, 0.80, 0.83),
    valis_non_rigid_D = c(5.1, 3.8, 3.2)
  ), file.path(d, "param_matrix.csv"))
  figs <- build_reg_figs(d)
  expect_true("05_valis_vs_overlap_agreement" %in% names(figs))
})

test_that("feature_dist/*.json builds the (legacy) distance-reduction figure when present", {
  skip_if_not_installed("jsonlite")
  d <- tmp_data()
  dir.create(file.path(d, "feature_dist"))
  jsonlite::write_json(list(
    moving_image = "P001_mov1",
    improvement  = list(distance_reduction_percent = 62.5)
  ), file.path(d, "feature_dist", "P001_mov1.json"), auto_unbox = TRUE)
  figs <- build_reg_figs(d)
  expect_true("03_feature_distance_reduction" %in% names(figs))
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `R_PROFILE_USER=/dev/null Rscript -e 'testthat::test_dir("tests/testthat", filter = "registration-accuracy-plots")'`
Expected: FAIL — `04_accuracy_vs_cost` / `05_valis_vs_overlap_agreement` / `03_feature_distance_reduction` not in names.

- [ ] **Step 3: Write minimal implementation**

In `code/registration_accuracy_plots.R`, insert before the final `figs` return (after the §2 block):

```r
  # -- §3 feature-distance improvement (OPTIONAL / LEGACY) ----------------------
  # mirage no longer emits feature_dist/*.json by default (the sweep uses reg_qc=2 + VALIS rTRE);
  # this renders only if a run set enable_feature_error. Needs jsonlite.
  fd_dir <- file.path(dir, "feature_dist")
  if (dir.exists(fd_dir) && requireNamespace("jsonlite", quietly = TRUE)) {
    jf <- list.files(fd_dir, pattern = "\\.json$", full.names = TRUE)
    if (length(jf)) {
      rows <- lapply(jf, function(j) {
        x <- tryCatch(jsonlite::fromJSON(j), error = function(e) NULL)
        if (is.null(x) || is.null(x$improvement$distance_reduction_percent)) return(NULL)
        data.frame(moving = x$moving_image %||% basename(j),
                   reduction_pct = as.numeric(x$improvement$distance_reduction_percent))
      })
      fd <- do.call(rbind, rows)
      if (!is.null(fd) && nrow(fd)) {
        figs[["03_feature_distance_reduction"]] <-
          ggplot(fd, aes(stats::reorder(moving, reduction_pct), reduction_pct)) +
          geom_col(fill = oi[3], width = .7) + coord_flip() +
          labs(title = "Feature-distance reduction after registration (legacy)",
               subtitle = "Per moving slide: percent drop in mean matched-feature distance (before → after).",
               x = NULL, y = "distance reduction (%)", caption = REG_CAPTION)
      }
    }
  }

  # -- §4 accuracy vs cost (Pareto) --------------------------------------------
  pm <- .reg_read_opt(dir, "param_matrix.csv")
  if (!is.null(pm) && all(c("reg_displacement_um_p50", "cpu_hours") %in% names(pm))) {
    pmf <- pm %>% dplyr::filter(is.finite(reg_displacement_um_p50), is.finite(cpu_hours))
    if (nrow(pmf)) {
      figs[["04_accuracy_vs_cost"]] <-
        ggplot(pmf, aes(cpu_hours, reg_displacement_um_p50)) +
        geom_point(size = 2, alpha = .8, colour = oi[1]) +
        labs(title = "Registration accuracy vs cost",
             subtitle = "Lower-left is better: less residual for fewer CPU-hours. One point per config.",
             x = "registration CPU-hours", y = "residual displacement, median (µm)",
             caption = REG_CAPTION)
    }
  }

  # -- §5 agreement of the two independent estimates ---------------------------
  # The paper's thesis: VALIS's own feature error and the segmentation-overlap Dice are computed by
  # DIFFERENT methods yet should track per run. Both are pre-joined in param_matrix.csv, so this is a
  # single scatter — no extra plumbing. Prefer the relative rTRE median, fall back to the distance.
  if (!is.null(pm) && "reg_dice_matched" %in% names(pm)) {
    valis_col <- intersect(c("valis_non_rigid_rTRE", "valis_non_rigid_D"), names(pm))[1]
    if (!is.na(valis_col)) {
      ag <- pm %>% dplyr::filter(is.finite(.data[[valis_col]]), is.finite(reg_dice_matched))
      if (nrow(ag) > 1) {
        figs[["05_valis_vs_overlap_agreement"]] <-
          ggplot(ag, aes(.data[[valis_col]], reg_dice_matched)) +
          geom_point(size = 3, alpha = .8, colour = oi[1]) +
          labs(title = "Registration accuracy: VALIS vs segmentation-overlap",
               subtitle = "Independent estimates per run — VALIS feature error (x) vs matched-nucleus Dice (y). They should track.",
               x = valis_col, y = "matched-nucleus Dice (reg_dice_matched)", caption = REG_CAPTION)
      }
    }
  }
```

Also add the null-coalescing helper near the top of the file (after `.reg_read_opt`), since base R lacks `%||%` before 4.4:

```r
`%||%` <- function(a, b) if (is.null(a) || length(a) == 0) b else a
```

- [ ] **Step 4: Run test to verify it passes**

Run: `R_PROFILE_USER=/dev/null Rscript -e 'testthat::test_dir("tests/testthat", filter = "registration-accuracy-plots")'`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add code/registration_accuracy_plots.R tests/testthat/test-registration-accuracy-plots.R
git commit -m ":sparkles: add §3 feature-distance (legacy) + §4 cost + §5 VALIS-vs-overlap agreement

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0149ze4SMDcvtmk817juvxXP"
```

---

### Task 4: `analysis/registration_accuracy.Rmd` page

**Files:**
- Modify (replace stub): `analysis/registration_accuracy.Rmd`

**Interfaces:**
- Consumes: `reg_figs` (from `code/registration_accuracy_plots.R`), `export_pdf_figures("registration_accuracy")` (from `code/pdf_export.R`).

- [ ] **Step 1: Replace the stub with the full page**

Overwrite `analysis/registration_accuracy.Rmd` with:

````markdown
---
title: "Registration accuracy (landmark-free, in-pipeline)"
author: "sceriff0"
date: "2026-07-24"
output: workflowr::wflow_html
editor_options:
  chunk_output_type: console
---

```{r setup, include=FALSE}
suppressPackageStartupMessages({
  library(tidyverse)
  library(here)
})
knitr::opts_chunk$set(echo = FALSE, message = FALSE, warning = FALSE,
                      dev = c("png", "pdf"))
source(here("code", "pdf_export.R"))                     # export_pdf_figures()
reg_dir  <- here("data", "benchmark")
have_reg <- any(file.exists(file.path(reg_dir,
  c("registration_valis_rtre.csv", "registration_accuracy.csv", "param_matrix.csv"))))
```

## Starting data and notation

Two **independent, landmark-free** estimates of how well the Mirage pipeline
registers each moving slide onto its reference, plus a cost view and an explicit
agreement check. Every figure is built by sourcing
`code/registration_accuracy_plots.R` and rendered inline from the CSVs in
`data/benchmark/` — no PNGs on disk. Figures whose input is absent are silently
skipped.

**Where the numbers come from.** All tables are produced by mirage
`benchmarks/analysis/make_tables.py` (the `paper_data/` set) — drop them into
`data/benchmark/` and re-knit.

- **`registration_valis_rtre.csv`** — VALIS's *own* feature error per moving
  slide (`original_rTRE` → `rigid_rTRE` → `non_rigid_rTRE`, with `n_matches`;
  some VALIS builds emit `*_D` raw distances instead). rTRE is **relative** (a
  fraction of the image diagonal), so it is unitless and comparable across image
  sizes. Auto-harvested by `make_tables.py` from the summary CSVs VALIS writes
  during `register()` — the same numbers the pipeline's QC report shows
  ("Registration Accuracy (Valis rTRE)").
- **`registration_accuracy.csv`** — an *independent* re-scoring by DAPI-nucleus
  overlap (`bin/warp_seg_qc.py`, `reg_qc=2`, harvested by `make_tables.py`). Per
  (run, moving, stage ∈ {native, rigid, non_rigid, micro}): `dice_matched` (areal
  Dice over matched nuclei) and `displacement_um_p50/p90` (centroid residual, in
  microns). This does not trust VALIS's features — a second method for the same
  question.
- **`param_matrix.csv`** — one wide row per run: the headline registration
  metrics (`reg_displacement_um_p50`, `reg_dice_matched`) and the VALIS median
  (`valis_non_rigid_D`/`valis_non_rigid_rTRE`) joined to cost (`cpu_hours`).
- **`feature_dist/*.json`** (optional/legacy) — before/after matched-feature
  distance per slide; only present if a run set `enable_feature_error`.

The finding to look for: the VALIS feature rTRE (§1) and the segmentation-overlap
Dice/displacement (§2) are computed by **different methods** yet should agree that
rigid → non-rigid registration tightens alignment — §5 plots that agreement
directly. Convergence of independent estimates is the point.

```{r build}
reg_figs <- list()
if (have_reg) {
  source(here("code", "registration_accuracy_plots.R"))
  cat(length(reg_figs), "figure(s) built from", basename(reg_dir), "data.\n")
} else {
  cat("No registration data found — add mirage `paper_data` CSVs",
      "(`registration_valis_rtre.csv` / `registration_accuracy.csv` /",
      "`param_matrix.csv`) to `data/benchmark/` and re-knit.")
}
```

```{r fig-notes}
fig_notes <- c(
  "01_valis_rtre_by_stage" = paste(
    "VALIS self-reported feature error (`registration_valis_rtre.csv`: `original_rTRE`,",
    "`rigid_rTRE`, `non_rigid_rTRE`, or the `*_D` distance variants), one line per moving",
    "slide across the stages. rTRE is relative to the image diagonal (unitless); lower = better."),
  "01b_valis_n_matches" = paste(
    "Feature-match count per slide (`n_matches`) — the evidence behind each rTRE.",
    "A slide with few matches has a low-confidence estimate."),
  "02_overlap_dice_by_stage" = paste(
    "Independent overlap check: `dice_matched` from `registration_accuracy.csv` per",
    "stage (native → rigid → non_rigid → micro), box across slides, faint per-slide",
    "lines. Computed from DAPI-nucleus overlap, not VALIS features; higher = better."),
  "02b_displacement_um_by_stage" = paste(
    "Matched-nucleus centroid residual in microns (`displacement_um_p50`/`_p90`) per",
    "stage. Physical units; lower = tighter alignment. Same source table as above."),
  "03_feature_distance_reduction" = paste(
    "(Legacy) percent drop in mean matched-feature distance before → after registration",
    "(`feature_dist/*.json`); only present if a run enabled `enable_feature_error`."),
  "04_accuracy_vs_cost" = paste(
    "Accuracy vs cost: `reg_displacement_um_p50` vs `cpu_hours` from `param_matrix.csv`,",
    "one point per config. Lower-left is better — less residual for fewer CPU-hours."),
  "05_valis_vs_overlap_agreement" = paste(
    "Agreement of the two independent estimates: VALIS feature error",
    "(`valis_non_rigid_rTRE`/`_D`) vs matched-nucleus Dice (`reg_dice_matched`) per run",
    "(`param_matrix.csv`). Two different methods; a clear trend = they corroborate each other.")
)
```

## Figures

Each figure is preceded by its derivation note. Figures whose CSV was not present
in `data/benchmark/` are silently absent.

```{r figures, results="asis", fig.width=9, fig.height=5.5}
if (length(reg_figs)) {
  for (nm in names(reg_figs)) {
    title <- gsub("_", " ", sub("^[0-9]+[a-z]?_", "", nm))
    cat("\n\n### ", title, "\n\n", sep = "")
    if (!is.na(fig_notes[nm])) cat(fig_notes[[nm]], "\n\n")
    print(reg_figs[[nm]])
    cat("\n\n")
  }
}
```

## Notes

- **Two independent methods.** §1 is VALIS grading itself from feature
  correspondences; §2 re-scores the same registrations by nucleus overlap. They
  are not derived from each other, so agreement (§5) is meaningful.
- **rTRE is relative, displacement is physical.** rTRE (§1) is a fraction of the
  image diagonal; `displacement_um` (§2) is in microns. Read §1 for cross-size
  comparability, §2 for an interpretable physical residual.
- Absolute-scale registration cost on synthetic sweep offsets is covered in the
  [pipeline benchmarks](benchmarks.html); this page uses the real patient slides.

```{r export-pdfs, include=FALSE}
export_pdf_figures("registration_accuracy")
```
````

- [ ] **Step 2: Verify it knits with no data (graceful skip)**

Run: `R_PROFILE_USER=/dev/null Rscript -e 'workflowr::wflow_build("analysis/registration_accuracy.Rmd", view = FALSE)'`
Expected: builds `docs/registration_accuracy.html` with the "No registration data found" message, no error. (If renv complains about missing pkgs, run without `R_PROFILE_USER=/dev/null`.)

- [ ] **Step 3: Verify it renders figures with synthetic data (local only, never committed)**

```bash
mkdir -p data/benchmark
R_PROFILE_USER=/dev/null Rscript -e '
  readr::write_csv(tibble::tibble(run_id="run0000", summary_csv="P001_summary.csv",
    img_name=c("P001_mov1","P001_mov2"),
    original_rTRE=c(50,40), rigid_rTRE=c(10,9), non_rigid_rTRE=c(5,4),
    n_matches=c(100,80)), "data/benchmark/registration_valis_rtre.csv")'
R_PROFILE_USER=/dev/null Rscript -e 'workflowr::wflow_build("analysis/registration_accuracy.Rmd", view = FALSE)'
```
Expected: HTML now shows "1 figure(s) built" and the rTRE slopegraph + n_matches panel. Then remove the synthetic data so nothing is committed:
```bash
rm -rf data/benchmark
```

- [ ] **Step 4: Commit** (the Rmd only — `data/` is gitignored; do not add `docs/` unless the repo commits rendered HTML — check `git status` first and match existing convention)

```bash
git add analysis/registration_accuracy.Rmd
git commit -m ":sparkles: build registration_accuracy analysis page (was empty stub)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0149ze4SMDcvtmk817juvxXP"
```

---

### Task 5: Reconcile `code/benchmark_plots.R` accuracy figures to current schema

**Files:**
- Modify: `code/benchmark_plots.R:288-307` (fig 11), `:310-320` (fig 12), `:412-426` (fig 17)
- Test: `tests/testthat/test-benchmark-plots-accuracy.R` (create)

**Interfaces:**
- `benchmark_plots.R` is a flat script that reads `measurements.csv` at source time (line 40) and errors if it is absent. The test drives the figure logic by writing a full tempdir fixture (`measurements.csv` + `param_matrix.csv`) and sourcing with `commandArgs`. Assert on the produced `bench_figs` after sourcing against a fixture dir.

- [ ] **Step 1: Write the failing test**

Create `tests/testthat/test-benchmark-plots-accuracy.R`:

```r
# Fig 11 must read the CURRENT mirage accuracy schema (param_matrix.csv,
# reg_displacement_um_p50) — not the retired quality.csv / reg_tre_median_px.
test_that("benchmark fig 11 reads param_matrix reg_displacement_um_p50", {
  d <- file.path(tempdir(), paste0("bench-", as.integer(Sys.time()), "-", sample(1e6, 1)))
  dir.create(d, recursive = TRUE, showWarnings = FALSE)
  # measurements.csv is read unconditionally at source time — minimal valid frame.
  readr::write_csv(tibble::tibble(
    run_id = "r1", process = "MIRAGE:REGISTER", peak_rss_gb = 1, realtime_s = 1,
    input_gb = 1, varied_axis = "baseline"), file.path(d, "measurements.csv"))
  readr::write_csv(tibble::tibble(
    run_id = c("r1", "r2"), cpu_hours = c(1.2, 2.5),
    reg_displacement_um_p50 = c(2.1, 1.4)), file.path(d, "param_matrix.csv"))
  bench_figs <- list()                    # sourced script fills this via save_fig
  commandArgs <- function(...) d          # shadow so `adir` resolves to the fixture
  source(here::here("code", "benchmark_plots.R"), local = TRUE)
  expect_true("11_accuracy_vs_cost" %in% names(bench_figs))
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `R_PROFILE_USER=/dev/null Rscript -e 'testthat::test_dir("tests/testthat", filter = "benchmark-plots-accuracy")'`
Expected: FAIL — fig 11 still keys off `quality.csv`/`reg_tre_median_px`, so `11_accuracy_vs_cost` is absent for a `param_matrix`-only fixture.

- [ ] **Step 3: Rewrite fig 11 (lines ~287-307)**

Replace the `## 11. REGISTRATION ACCURACY vs COST` block with:

```r
# ── 11. REGISTRATION ACCURACY vs COST (the Pareto view) ──
# Current mirage emits the headline registration residual pre-joined to cost in
# param_matrix.csv (reg_displacement_um_p50, physical µm). The old quality.csv/
# reg_tre_median_px is retired upstream.
pm  <- read_opt("param_matrix.csv")
if (!is.null(pm) && all(c("reg_displacement_um_p50", "cpu_hours") %in% names(pm))) {
  ac <- pm %>% filter(is.finite(reg_displacement_um_p50), is.finite(cpu_hours))
  if (nrow(ac) > 0) {
    p11 <- ac %>%
      ggplot(aes(cpu_hours, reg_displacement_um_p50)) +
      geom_point(aes(colour = if ("memory_mode" %in% names(ac)) memory_mode else NULL),
                 size = 3, alpha = .8) +
      { if ("memory_mode" %in% names(ac))
          scale_colour_manual(values = oi, name = "memory_mode", na.translate = FALSE) } +
      labs(title = "Registration accuracy vs cost",
           subtitle = "Lower-left is better: less residual for fewer CPU-hours. Each point is a config.",
           x = "registration CPU-hours", y = "residual displacement, median (µm)")
    save_fig(p11, "11_accuracy_vs_cost", 8, 5)
  }
}
```

- [ ] **Step 4: Repurpose fig 17 (lines ~411-426) — was classic-vs-distributed, now agreement**

The distributed/tiled registration path was removed from mirage, so `reg_distributed_tiling` /
`reg_dist_force_tiling` no longer exist and the old "error by path" figure would always skip.
Replace the `## 17. REGISTRATION ERROR by path` block with a VALIS-vs-segmentation **agreement**
view (both signals are in `param_matrix.csv`):

```r
# ── 17. VALIS vs SEGMENTATION-OVERLAP agreement (two independent accuracy estimates) ──
# Single registration path now, so the classic/separated/tiled comparison is retired. Instead show
# that VALIS's own feature error and the DAPI-overlap Dice agree per run — both live in param_matrix.
if (!is.null(pm) && "reg_dice_matched" %in% names(pm)) {
  valis_col <- intersect(c("valis_non_rigid_rTRE", "valis_non_rigid_D"), names(pm))[1]
  if (!is.na(valis_col)) {
    ag <- pm %>% filter(is.finite(.data[[valis_col]]), is.finite(reg_dice_matched))
    if (nrow(ag) > 1) {
      p17 <- ggplot(ag, aes(.data[[valis_col]], reg_dice_matched)) +
        geom_point(size = 3, alpha = .8, colour = oi[1]) +
        labs(title = "Registration accuracy: VALIS vs segmentation-overlap",
             subtitle = "Independent estimates per run — VALIS feature error (x) vs matched-nucleus Dice (y). They should track.",
             x = valis_col, y = "matched-nucleus Dice (reg_dice_matched)")
      save_fig(p17, "17_valis_vs_overlap_agreement", 8, 5)
    }
  }
}
```

- [ ] **Step 5: Repoint fig 12 (lines ~309-320)**

Replace the `## 12. SEGMENTATION METHOD QUALITY` cell-count block with a version reading `param_matrix.csv` (`n_cells` + `seg_method` — mirage carries `seg_method` in `param_matrix`/`runs_master`), degrading to an unfaceted distribution if `seg_method` is unavailable:

```r
# ── 12. SEGMENTATION cell counts by method ──
# n_cells and seg_method both live in param_matrix.csv now (make_tables carries seg_method through);
# fall back to runs_master.csv only if a stripped param_matrix lacks seg_method.
rm_tbl <- read_opt("runs_master.csv")
sc <- NULL
if (!is.null(pm) && "n_cells" %in% names(pm)) {
  sc <- pm
  if (!("seg_method" %in% names(sc)) && !is.null(rm_tbl) && "seg_method" %in% names(rm_tbl))
    sc <- sc %>% left_join(rm_tbl %>% select(any_of(c("run_id", "seg_method"))), by = "run_id")
  sc <- sc %>% filter(is.finite(n_cells))
}
if (!is.null(sc) && nrow(sc) > 0) {
  if ("seg_method" %in% names(sc)) {
    p12 <- ggplot(sc, aes(seg_method, n_cells, colour = seg_method)) +
      geom_boxplot(outlier.shape = NA, width = .5) + geom_jitter(width = .12, alpha = .5) +
      scale_colour_manual(values = oi, guide = "none") +
      labs(title = "Segmentation: cells detected per method",
           subtitle = "Spread = each method's own parameter sweep. Large gaps = methods disagree on cell count.",
           x = NULL, y = "cells detected (max mask label)")
  } else {
    p12 <- ggplot(sc, aes("all runs", n_cells)) +
      geom_boxplot(outlier.shape = NA, width = .4) + geom_jitter(width = .1, alpha = .5) +
      labs(title = "Segmentation: cells detected",
           subtitle = "seg_method unavailable — pooled distribution.",
           x = NULL, y = "cells detected (max mask label)")
  }
  save_fig(p12, "12_segmentation_cell_counts", 8, 5)
}
```

Note: `rm_tbl <- read_opt("runs_master.csv")` is now defined in the fig-12 block; the retired fig-17
no longer needs it. Also delete the now-orphaned `qual <- read_opt("quality.csv")` at the old line
288 **only if** no surviving figure references `qual` — fig 12b (`segmentation_agreement.csv`) does
not; verify with `grep -n "qual" code/benchmark_plots.R` and remove dead reads.

- [ ] **Step 6: Verify fig 12b unchanged and run tests**

Run: `grep -n "segmentation_agreement\|instance_f1\|qual\b" code/benchmark_plots.R`
Expected: fig 12b still reads `segmentation_agreement.csv` (`instance_f1`, `cell_count_ratio`) — leave as-is. No dangling `qual` reads remain.

Run: `R_PROFILE_USER=/dev/null Rscript -e 'testthat::test_dir("tests/testthat", filter = "benchmark-plots-accuracy")'`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add code/benchmark_plots.R tests/testthat/test-benchmark-plots-accuracy.R
git commit -m ":recycle: reconcile benchmark accuracy figs to current mirage schema

quality.csv/reg_tre_median_px is retired upstream (fig 11 -> param_matrix.csv,
reg_displacement_um_p50 µm); the distributed path was removed, so fig 17 is
repurposed from classic-vs-distributed error-by-path into a VALIS-vs-overlap
agreement scatter; fig 12 reads param_matrix n_cells/seg_method.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0149ze4SMDcvtmk817juvxXP"
```

---

### Task 6: Reconcile `benchmarks.Rmd` prose + cross-links; tidy index

**Files:**
- Modify: `analysis/benchmarks.Rmd` (fig_notes 11/17, optional-CSV list ~line 72-77, warning block ~58-66, Notes ~211-216)
- Modify: `analysis/index.Rmd` (registration-accuracy bullet)

**Interfaces:** none (prose only).

- [ ] **Step 1: Update the two fig_notes**

In `analysis/benchmarks.Rmd`, replace the `"11_accuracy_vs_cost"` note with:

```r
  "11_accuracy_vs_cost" = paste(
    "Accuracy vs cost (lower-left is better). `reg_displacement_um_p50` (matched-nucleus",
    "centroid residual, µm) from `param_matrix.csv` vs `cpu_hours`, one point per config.",
    "For the real-slide, landmark-free accuracy detail see the",
    "[registration accuracy](registration_accuracy.html) page."),
```

Replace the `"17_registration_error_by_path"` note — rename the key to
`"17_valis_vs_overlap_agreement"` and set:

```r
  "17_valis_vs_overlap_agreement" = paste(
    "Agreement of two independent registration-accuracy estimates from `param_matrix.csv`:",
    "VALIS's own feature error (`valis_non_rigid_rTRE`/`_D`, x) vs the DAPI-overlap",
    "`reg_dice_matched` (y), one point per run. They are computed by different methods, so a",
    "clear trend means they corroborate each other. (Replaces the retired classic-vs-distributed",
    "error-by-path view — mirage now has a single registration path.)"),
```

- [ ] **Step 2: Update the optional-CSV list (~lines 72-77)**

Replace the "Optional CSVs" paragraph so it names the current tables (distributed CSVs removed). New text:

```markdown
**Optional CSVs** (a figure needing an absent file is skipped): `resource_stats.csv`,
`param_matrix.csv`, `registration_accuracy.csv`, `registration_valis_rtre.csv`,
`run_cost.csv`, `segmentation_agreement.csv`, `runs_master.csv`. Accuracy columns
(`reg_displacement_um_p50`, `reg_dice_matched`, `valis_non_rigid_D`/`_rTRE`,
`instance_f1`, `cell_count_ratio`, `peak_rss_gb_mean`/`_std`) are **precomputed
upstream** by mirage `make_tables.py` and only read here.
```

- [ ] **Step 3: Fix the warning caveat block (~lines 58-66)**

Replace the second half of the `!!! warning` block. Remove the `reg_tre_median_px`-proxy sentence and the ANHIR/ACROBAT deferral sentence. New ending for that block:

```markdown
    **The sweep images are synthetic.** Extra channels and the moving registration
    panels are *synthesized* from channel 0 (duplicate + intensity jitter + a fixed
    pixel shift), so figures that touch channels or registration *accuracy* on sweep
    cells reflect a known synthetic offset, **not** biological registration
    difficulty. Treat the accuracy figures (11, 17) as relative cost-vs-error and
    method-agreement views on synthetic offsets. Landmark-free accuracy on the
    **real** patient slides is reported separately on the
    [registration accuracy](registration_accuracy.html) page.
```

- [ ] **Step 4: Update the Notes bullet (~lines 211-216)**

Replace the "Accuracy metrics are read, not recomputed" bullet's column names (distributed drift columns removed):

```markdown
- **Accuracy metrics are read, not recomputed.** `reg_displacement_um_p50` /
  `reg_dice_matched` (registration residual and matched-nucleus Dice),
  `valis_non_rigid_D`/`_rTRE` (VALIS's own feature error),
  `instance_f1`/`cell_count_ratio` (segmentation agreement), and the replicate
  mean/sd all arrive precomputed in their CSVs. The reductions `benchmark_plots.R`
  performs itself are the log-space OLS (β, R²), the per-run max/sum over
  `reg_leaves`, and replicate means/shares.
```

- [ ] **Step 5: Enrich the index bullet**

In `analysis/index.Rmd`, replace the registration-accuracy bullet with:

```markdown
- [Registration accuracy](registration_accuracy.html) — landmark-free accuracy of
  the image registration on the real patient slides: VALIS feature rTRE per stage,
  an independent segmentation-overlap (Dice / µm displacement) cross-check, and an
  explicit agreement view of the two.
```

- [ ] **Step 6: Verify both pages knit**

Run: `R_PROFILE_USER=/dev/null Rscript -e 'workflowr::wflow_build(c("analysis/benchmarks.Rmd","analysis/index.Rmd"), view = FALSE)'`
Expected: both build with no error (benchmarks skips figures on absent data; no missing-column crash).

- [ ] **Step 7: Commit**

```bash
git add analysis/benchmarks.Rmd analysis/index.Rmd
git commit -m ":memo: reconcile benchmark prose to current accuracy schema; link reg-accuracy page

Drops the reg_tre_median_px-proxy and ANHIR/ACROBAT-deferral caveats and the
retired distributed-drift columns; names param_matrix.csv/registration_accuracy.csv/
registration_valis_rtre.csv in the CSV list; repoints fig-17 note to the VALIS-vs-
overlap agreement view; cross-links the new registration_accuracy page.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0149ze4SMDcvtmk817juvxXP"
```

---

## Self-Review

**Spec coverage:**
- §1 VALIS rTRE (from `registration_valis_rtre.csv`) → Task 1. §2 overlap Dice/displacement → Task 2. §3 feature-distance (optional/legacy) → Task 3. §4 accuracy-vs-cost → Task 3. §5 VALIS-vs-overlap agreement → Task 3. Page + prose/derivation notes → Task 4. Part B fig 11 repointed / fig 17 repurposed / fig 12 repointed → Task 5; 12b verified → Task 5 Step 6. Part B prose/caveat/ANHIR-removal/distributed-removal/cross-link → Task 6. Index → Task 6. Verification approach (source parses, knit graceful, synthetic render) → Tasks 1-6 steps. All spec sections mapped.
- Data contract updated to mirage `bench/reconcile-main`: VALIS rTRE auto-emitted; distributed columns/CSVs removed; feature-distance JSON optional/legacy.

**Placeholder scan:** No TBD/TODO. Every code step shows complete code. The rTRE column-name uncertainty (rTRE vs `_D`, id column) is handled at runtime by detection (`grep("_rTRE$")` → fallback `_D`; `intersect(c("img_name","name","filename","summary_csv"))`) rather than a hard-coded schema — Task 1 Step 3.

**Type consistency:** `build_reg_figs(dir)` signature and figure keys (`01_valis_rtre_by_stage`, `01b_valis_n_matches`, `02_overlap_dice_by_stage`, `02b_displacement_um_by_stage`, `03_feature_distance_reduction`, `04_accuracy_vs_cost`, `05_valis_vs_overlap_agreement`) are identical across Tasks 1-4 and the tests. `.reg_read_opt` used consistently. `save_fig`/`bench_figs`/`read_opt` in Task 5 match the existing names in `benchmark_plots.R`. `%||%` defined before first use (Task 3). No reference to `reg_distributed_tiling`/`reg_dist_*`/`quality.csv`/`reg_tre_median_px`/`classic_vs_distributed_registration.csv`/`registration_drift.csv` survives.
