# Containers

<p class="standfirst">Which of the ten images a process actually runs in, and which version of
which imaging library that image installs. Read this before touching a Dockerfile, adding a
process, or debugging output that differs between two processes that both call
<code>tifffile</code>.</p>

!!! abstract "Canonical sources"
    - **Build contexts** — `containers/<name>/Dockerfile` (+ `requirements.txt` where one exists)
    - **Process → image** — each `modules/local/*.nf`'s `container:` directive, plus
      `lib/SegBackends.groovy` (`SEGMENT`) and `lib/WarpBackends.groovy` (`WARP_SEG_QC`) for the
      two processes that pick their image at runtime
    - **Machine-checked** — `tests/test_container_pins.py` asserts every `container:` directive
      resolves to one of the ten images below (or the two allowlisted external ones), and that
      every image pins `tifffile` explicitly

This mapping did not exist as a single table anywhere in the repo before 2026-08-14 (Task 11b of
the dataflow architecture review) — it had to be reconstructed by hand from three sources: every
`container:` directive in `modules/local/*.nf`, the two backend-dispatch tables, and every
Dockerfile / `requirements.txt` under `containers/`. It should not have to be reconstructed again;
update this page (and `tests/test_container_pins.py`'s `DIR_TO_TAG`) in the same change that adds,
renames, or retires an image.

!!! danger "A merged Dockerfile edit changes nothing at runtime by itself"
    CI's image-build workflow (`.github/workflows/build-images.yml`) does **not** push on `main` —
    only on a release or a manual `workflow_dispatch`. So a pin change on this page and in the
    Dockerfiles below can merge, go green, and still be running the *old* image on every cluster
    until someone deliberately rebuilds and pushes
    `bolt3x/attend_image_analysis:<tag>`. See `containers/README.md`'s reproducibility caveat: the
    tags are mutable, so even a pushed image is not byte-for-byte pinned by tag alone.

---

## The ten images

| `containers/<dir>` | Published tag | Processes | Entrypoint script(s) |
|---|---|---|---|
| `bioformats` | `convert_bioformats_2` | `CONVERT_IMAGE` | `convert_image.py` |
| `preprocess` | `preprocess` | `PREPROCESS`, `SPLIT_CHANNELS`, `GENERATE_QC_REPORT`, `GENERATE_PREPROCESS_QC` | `preprocess.py`, `split_multichannel.py`, `generate_qc_report.py`, `generate_preprocess_qc.py` |
| — *(not vendored)* | `cdgatenbee/valis-wsi:1.0.0` (upstream) | `REGISTER`; `WARP_SEG_QC` (valis backend) | `register.py`, `warp_seg_qc.py` |
| `merge` | `merge` | `MERGE_AND_PYRAMID`, `EXTRACT_MASK_SERIES` | `merge_channels_pyramid.py`, `extract_mask_series.py` |
| `tiled` | `tiled` | `TILED_STITCH`, `TILED_SOLVE`, `TILED_COARSE`, `TILED_REG_TILE`; `WARP_SEG_QC` (tiled backend) | `tiled_stitch.py`, `tiled_solve.py`, `tiled_coarse.py`, `tiled_reg_tile.py`, `warp_seg_qc.py` |
| `quantification` | `quantification_gpu` | `EXPORT_GEOJSON`, `EXTRACT_CELL_PROPERTIES`, `EXTRACT_NUCLEI_PROPERTIES`, `QUANTIFY`, `MERGE_QUANT_CSVS`, `GENERATE_POSTPROCESSING_QC`, `SEG_QC_GEOJSON` | `export_geojson.py`, `extract_cell_properties.py` (both properties processes), `quantify.py`, `merge_quant_csvs.py`, `generate_postprocessing_qc.py`, `mask_to_geojson.py` |
| `segmentation` | `segmentation_gpu` | `SEGMENT` / `SEG_QC_SEGMENT` when `--seg_method stardist` | `segment.py` |
| `istantseg` *(dir name is a preserved historical typo — see `containers/README.md`)* | `instant_seg` | `SEGMENT` / `SEG_QC_SEGMENT` when `--seg_method instantseg` *(default)* | `segment_instantseg.py` |
| `cellsam` | `cellsam` | `SEGMENT` / `SEG_QC_SEGMENT` when `--seg_method cellsam` | `segment_cellsam.py` |
| `debug_diffeo` | `debug_diffeo` | `GENERATE_REGISTRATION_QC` | `generate_registration_qc.py` (uses `bin/utils/qc.py`) |
| `spatialdata` | `spatialdata` | `EXPORT_SPATIALDATA`; the out-of-band `join_flowpath.py` cohort join | `export_spatialdata.py`, `join_flowpath.py` |

`ubuntu:22.04` (`AGGREGATE_SIZE_LOGS`) is deliberately not in this table: it runs no imaging
library at all (coreutils only), so it is allowlisted in
`tests/test_container_pins.py::EXTERNAL_ALLOWLIST` rather than given a row here.

---

## `tifffile` pin convergence (Task 11b, 2026-08-14)

Before this task, `tifffile` was pinned to five different exact versions, three images pinned
nothing at all, and a fourth pinned nothing explicit but had an *implicit* version ceiling coming
from another package. Same shared writer code (task 11 gives image I/O one owner), five different
`tifffile` behaviours underneath it — a latent inconsistency, not a bug that had fired yet.

| Image | `tifffile` pin | Live pin source | Status |
|---|---|---|---|
| `tiled` | `==2024.7.2` | `requirements.txt` | Convergence anchor — already pinned, paired with `zarr==2.18.3` |
| `merge` | `==2024.7.2` | `Dockerfile` (inline) | **Changed**: was unpinned, now converges with `tiled` |
| `quantification_gpu` | `==2024.7.2` | `Dockerfile` (inline) | **Changed**: was unpinned, now converges with `tiled` |
| `instant_seg` | `==2024.7.2` | `Dockerfile` (inline) | **Changed**: had no pin at all (transitive via scikit-image), now converges with `tiled` |
| `convert_bioformats_2` | `==2024.7.24` | `Dockerfile` (inline) | Left as-is — no build available to verify a bump |
| `preprocess` | `==2023.7.10` | `Dockerfile` (inline) | Left as-is — pinned alongside `numpy==1.24.3`, `scikit-image==0.21.0`, `jax[cpu]==0.4.20` |
| `cellsam` | `==2023.4.12` | `Dockerfile` (inline) | Left as-is — shares the value with `debug_diffeo` |
| `debug_diffeo` | `==2023.4.12` | `requirements.txt` | Left as-is — shares the value with `cellsam` |
| `segmentation_gpu` | `==2023.2.28` | `Dockerfile` (inline) | **Changed**: was implicit-only; genuinely cannot converge (see below) |
| `spatialdata` | `>=2024.7.2` | `requirements.txt` | Left as-is — deliberate floor, not a hard pin (see the zarr section) |

Net result: **five** distinct values across ten images (was five pinned values *plus* three
unpinned *plus* one implicit — eight distinct states). The four-way convergence on `2024.7.2`
(`tiled`, `merge`, `quantification_gpu`, `instant_seg`) is the real win; the other five images kept
their pre-existing value because bumping an *already-pinned* dependency without a build available
to verify it against that image's other pinned packages is exactly the "wrong pin is worse than no
pin" trap this task was warned against.

### Why `segmentation_gpu` cannot join the convergence

`segmentation_gpu` pins `aicsimageio==4.14.0`. That package's own published PyPI metadata for that
exact release declares:

```
tifffile<2023.3.15,>=2021.8.30
```

That is a **ceiling**, not just a floor — every other value in the table above
(`2023.4.12`, `2023.7.10`, `2024.7.x`) is above it. So `tifffile==2023.2.28` (the newest release
that still satisfies `<2023.3.15`, confirmed against PyPI's published release list) is the only
value that can go in this image without also removing `aicsimageio` — which this task did not do
here (see [Noticed, not fixed](#noticed-not-fixed) below). `scikit-image==0.25.2`, also pinned in
this image, only floors `tifffile` at `>=2022.8.12`, so it does not further constrain the choice.

### Why `instant_seg` needed a pin explained, not just added

`instanseg-torch`'s and `imagecodecs`' own base installs do **not** depend on `tifffile` at all —
only under optional extras (`full`, `io`, `test`) that this image's Dockerfile never requests.
`tifffile` reached this image only transitively, through `instanseg-torch`'s unconditional
`scikit-image>=0.21.0`, which itself floors `tifffile` at `>=2022.8.12` with no ceiling. That is
why the image had *zero* explicit `tifffile` mention before this task, not merely a missing pin —
and why `2024.7.2` is a safe convergence target for it.

---

## The `zarr` split — left alone, deliberately

`tiled` pins `zarr==2.18.3`; `spatialdata` pins `zarr>=3.0.0`. These are incompatible, and the two
images are kept separate **specifically so neither constraint has to yield**:

- `spatialdata>=0.8.0` requires `zarr>=3` (it writes NGFF `"0.5-dev-spatialdata"`).
- `tiled`'s streaming gigapixel stitch depends on `tifffile`'s `aszarr` lazy-region-read path
  against `zarr==2.18.3` specifically (`numcodecs==0.13.1` is pinned alongside it for the same
  reason: newer `numcodecs` drops the `blosc.cbuffer_sizes` symbol `zarr` 2.18 imports).

This is documented in three places, and this task did not touch any of them: the header comment in
`containers/spatialdata/Dockerfile`, the inline comments on both `zarr` lines in
`containers/*/requirements.txt`, and `containers/README.md`'s "zarr major versions differ on
purpose" note. Task 11b's brief was explicit that reconciling this split is **out of scope** — it
is a real, known, worked-around constraint, not an oversight.

---

## `quantification_gpu`: `cupy` / `cucim` / `aicsimageio[all]` removed

`containers/quantification/Dockerfile` installed `cupy-cuda12x`, `cucim`, `aicsimageio` and
`aicsimageio[all]` (both the bare package and the `[all]` extra — a redundant pair even on their
own terms). None of them were imported, directly or transitively through this repo's own code, by
any script that runs in this container.

**Check run**: every process this image serves —
`EXPORT_GEOJSON`, `EXTRACT_CELL_PROPERTIES`, `EXTRACT_NUCLEI_PROPERTIES`, `QUANTIFY`,
`MERGE_QUANT_CSVS`, `GENERATE_POSTPROCESSING_QC`, `SEG_QC_GEOJSON` — traced to its entrypoint
script (`export_geojson.py`, `extract_cell_properties.py`, `quantify.py`, `merge_quant_csvs.py`,
`generate_postprocessing_qc.py`, `mask_to_geojson.py`) and its shared `bin/utils/*.py` helpers
(`image_utils.py`, `logger.py`, `measurements.py`, `metadata.py`, `validation.py`), then:

```bash
grep -rn "cupy\|cucim\|aicsimageio" bin/
```

returned **zero matches** anywhere under `bin/` (only the Dockerfile itself mentioned them). All
four packages were removed; `tifffile==2024.7.2` was added explicitly in the same edit. If a future
script in this container starts using GPU-accelerated cuCIM/CuPy image ops or AICSImageIO's
multi-format reader, re-add the specific package it needs rather than restoring the whole set
"just in case" — that dead-weight accumulation is exactly what happened here.

---

## Noticed, not fixed

Found while reconstructing this mapping; explicitly out of Task 11b's scope (the brief named only
`quantification_gpu`'s `cupy`/`cucim`/`aicsimageio[all]` for removal), left for a follow-up task:

- **`segmentation_gpu` also never imports `aicsimageio`.** `grep -n "aicsimageio" bin/segment.py`
  (the only script this image runs) returns nothing — the same "installed but unused" pattern as
  `quantification_gpu`, except here it is not dead weight only: its own version pin
  (`tifffile<2023.3.15`) is the reason `segmentation_gpu` cannot join the four-way `tifffile`
  convergence above. Removing it would very likely let `segmentation_gpu` converge on `2024.7.2`
  too — but that is a build-verified change this task's hard constraint (no image builds available)
  cannot respect, so it is reported rather than made.
- **`containers/segmentation/requirements.txt` is dead.** It pins `tifffile==2023.4.12` (plus
  `dipy`, `opencv-python`, etc. — it is a byte-for-byte copy of `containers/debug_diffeo/requirements.txt`),
  but `containers/segmentation/Dockerfile` never `COPY`s or `pip install -r`s it. The file has no
  effect on the built image; the real (and, after this task, correctly pinned) `tifffile` version
  for `segmentation_gpu` is the one inline in the Dockerfile, `2023.2.28`. `tests/test_container_pins.py`
  detects and accounts for this (see `REQUIREMENTS_WIRED_RE`) rather than validating the dead file
  as if it were live.
- **`containers/merge`, `containers/quantification` and `containers/istantseg` pin only `tifffile`
  explicitly** — `numpy`, `numba`, `imagecodecs`, `zarr` (in `merge`), `scikit-image`, `scipy`,
  `pandas` (in `quantification`) remain unpinned in those two Dockerfiles, so those images still
  float on every rebuild for everything except the one library this task was scoped to.
- **`containers/README.md`'s VALIS section still describes a `reg_dist_container` /
  `mirage_valis_1.0.0` distributed tiled-VALIS path** (`params.reg_dist_container`,
  `build-valis-image.yml`). A repo-wide grep found no live reference to either — the distributed
  tiled-VALIS path was archived; the *separate*, still-live STARE `tiled` method
  (`registration_method='tiled'`) is unrelated. That paragraph in `containers/README.md` appears
  stale.

---

## See also

- [Resources → Containers](resources.md#containers) — the per-process resource table this page's
  process↔image mapping complements
- `containers/README.md` — build/publish mechanics, Docker Hub tag conventions, the `procps`
  requirement every image must satisfy
- `tests/test_container_pins.py` — the machine-checked guard for both tables on this page
