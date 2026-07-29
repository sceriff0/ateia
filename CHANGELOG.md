# Changelog

All notable changes to the MIRAGE pipeline will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-29

First public release. End-to-end multiplex WSI processing: preprocessing, registration,
segmentation, per-cell quantification, optional phenotyping, and QuPath-compatible
GeoJSON + pyramidal OME-TIFF export.

### Added
- **Preprocessing** — BaSiC illumination correction with FOV tiling; Bio-Formats/ND2 →
  OME-TIFF conversion; multi-channel parallel processing.
- **Registration** — two backends selected by `--registration_method`:
  - `valis` (default): VALIS graph-based whole-slide alignment, with optional staged
    micro-registration (`reg_micro_reg` = 0/1/2).
  - `tiled`: STARE — JVM-free, internally tiled, fully parallel registration
    (`reg_tiled_*` params), usable on a laptop.
- **Staged registration QC** (`reg_qc` = 0/1/2) — DAPI overlay plus segmentation-overlap
  dice/displacement attributed per registration stage.
- **Segmentation** — three backends via `--seg_method`: StarDist (default), InstanSeg,
  and CellSAM; GPU or CPU; configurable nuclei→whole-cell expansion.
- **Quantification** — per-cell morphology and per-channel intensity; optional
  per-compartment (Nucleus / Cytoplasm / Cell) signal and expanded Mean/Sum statistics.
- **Phenotyping (optional, panel-agnostic)** — conformal-risk-controlled cell-type
  assignment when a panel is supplied (`panel_spec` / `panel_model`); skipped by default.
- **Reference-free segmentation-quality evaluation** — CellSegmentationEvaluator
  `QualityScore` with a `cse_max_pixels` downsample cap.
- **Export** — QuPath-compatible `cells.geojson` and pyramidal OME-TIFF; configurable
  contour simplification (`simplify_tolerance`) and coordinate precision.
- **Incremental cyclic-IF** (`--mode add_cycle`) — fold a new imaging cycle into a
  completed patient run, reusing the prior reference, segmentation mask, and old-marker
  quantification; recomputes only the new cycle.

### Infrastructure
- DSL2 Nextflow architecture with step-based execution (`--start` / `--stop`).
- Checkpoint-based restart (preprocessing, registration, postprocessing).
- SLURM/Singularity and Docker execution profiles.
- Per-process version tracking, aggregated QC report, and computational-resource report.
- JSON-schema parameter definitions plus Groovy validation utilities.

[1.0.0]: https://github.com/sceriff0/mirage/releases/tag/v1.0.0
