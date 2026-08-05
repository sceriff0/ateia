# Mirage Container Images

This directory vendors the Docker build contexts for every image the Mirage
pipeline runs. Each subdirectory is a self-contained build context
(`containers/<name>/Dockerfile` plus any `requirements.txt` / helper files).

Images are built and published to **Docker Hub**, in a single public repository
using one descriptive tag per image:

```
bolt3x/attend_image_analysis:<tag>
```

Publishing is automated by [`.github/workflows/build-images.yml`](../.github/workflows/build-images.yml)
and the release flow in [`.github/workflows/release.yml`](../.github/workflows/release.yml).
CI authenticates with the `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` repository
secrets. Docker Hub images are **public**, so no pull credentials are needed on
the HPC/cluster side (unlike GHCR, whose default-private packages caused
`403 Forbidden` on `singularity pull`).

> `<tag>` is a content-descriptive tag (e.g. `preprocess`,
> `convert_bioformats_2`, `segeval`) — not a release version. The modules pin
> these tags directly and never use `:latest`.
>
> **Reproducibility caveat:** these descriptive tags are **mutable** — rebuilding and
> re-pushing `bolt3x/attend_image_analysis:preprocess` changes what a checkout runs, so
> a tagged pipeline release is **not** byte-for-byte reproducible from the tag alone. For
> a hard reproducibility guarantee, pin by immutable digest (`@sha256:…`) or cut
> release-versioned image tags (e.g. `:v1.0.0`) at release time.

## Image mapping

| Build context (`containers/<name>/`) | Docker Hub image:tag | Pipeline process(es) that use it | Source / base image |
| --- | --- | --- | --- |
| `bioformats` | `bolt3x/attend_image_analysis:convert_bioformats_2` | `CONVERT_IMAGE` | `eclipse-temurin:21-jre-jammy` + Glencoe `bioformats2raw` 0.12.0 / `raw2ometiff` 0.10.0 + `tifffile`/`numpy` |
| `preprocess` | `bolt3x/attend_image_analysis:preprocess` | `PREPROCESS`, `GENERATE_PREPROCESS_QC`, `GENERATE_QC_REPORT`, `SPLIT_CHANNELS` (4 modules) | `ubuntu:22.04` + Python 3.11 + BaSiCPy/JAX(cpu)/scikit-image illumination-correction stack |
| `quantification` | `bolt3x/attend_image_analysis:quantification_gpu` | `QUANTIFY`, `EXTRACT_CELL_PROPERTIES`, `EXTRACT_NUCLEI_PROPERTIES`, `EXPORT_GEOJSON`, `GENERATE_POSTPROCESSING_QC` (+ `quantify.nf` second container directive) (6 modules) | `nvidia/cuda:12.2.2-devel-ubuntu22.04` + cupy/cucim GPU quantification stack |
| `segmentation` | `bolt3x/attend_image_analysis:segmentation_gpu` | `SEGMENT` (default backend, `params.seg_method` = stardist) | `tensorflow/tensorflow:2.15.0-gpu-jupyter` + StarDist 0.9.1 / Cellpose 3.1.1.1 |
| `cellsam` | `bolt3x/attend_image_analysis:cellsam` | `SEGMENT` (`params.seg_method` = `cellsam`) | `pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime` + `cellSAM` (git) |
| `istantseg` | `bolt3x/attend_image_analysis:instant_seg` | `SEGMENT` (`params.seg_method` = `instantseg`) | `pytorch/pytorch:2.5.1-cuda11.8-cudnn9-runtime` + `instanseg-torch` |
| `merge` | `bolt3x/attend_image_analysis:merge` | `MERGE_AND_PYRAMID` | `pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime` + tifffile/imagecodecs pyramid stack |
| `debug_diffeo` | `bolt3x/attend_image_analysis:debug_diffeo` | `GENERATE_REGISTRATION_QC` | `nvidia/cuda:12.2.2-cudnn8-devel-ubuntu22.04` + Miniconda/bftools + StarDist/cudipy diffeo QC stack |
| `segeval` | `bolt3x/attend_image_analysis:segeval` | `SEG_QUALITY_EVAL`, `MERGE_SEG_EVAL` | `python:3.11-slim` + numpy/scipy/pandas/scikit-image/scikit-learn/aicsimageio/tifffile/xmltodict (vendored CSE metrics) |
| `tiled` | `bolt3x/attend_image_analysis:tiled` | `TILED_REGISTER`, `WARP_SEG_QC_TILED` (STARE `registration_method='tiled'`) | `python:3.11-slim` + numpy/scipy/scikit-image/tifffile — **no JVM/BioFormats/libvips/GPU** (~438 MB, vs the multi-GB VALIS image) |
| `spatialdata` | `bolt3x/attend_image_analysis:spatialdata` | `EXPORT_SPATIALDATA` (+ the out-of-band `bin/join_flowpath.py` cohort join) | `python:3.11-slim` + spatialdata/anndata/geopandas/zarr 3 — CPU only, no JVM/GPU |
| VALIS (not vendored) | `cdgatenbee/valis-wsi:1.0.0` (upstream) | `REGISTER`, `ESTIMATE_FEATURE_DISTANCES` | upstream maintained image — **not rebuilt or published by us** (see note below) |

> **zarr major versions differ on purpose.** `spatialdata` pins `zarr>=3.0.0`
> (required by `spatialdata>=0.8.0`, which writes NGFF `"0.5-dev-spatialdata"`),
> while `tiled` pins `zarr==2.18.3` for `tifffile`'s `aszarr` region reads. Keeping
> them in separate images is what lets both constraints hold without either being
> downgraded.

> The context directory name `istantseg` (a historical typo) is preserved
> verbatim so it matches the upstream build context and the legacy DockerHub tag
> `bolt3x/attend_image_analysis:instant_seg`. The published GHCR image is
> therefore `.../istantseg`, even though `params.seg_method` is `instantseg`.

### VALIS — uses the upstream image (not vendored)

`REGISTER` and `ESTIMATE_FEATURE_DISTANCES` reference the maintained upstream
image [`cdgatenbee/valis-wsi:1.0.0`](https://hub.docker.com/r/cdgatenbee/valis-wsi)
directly. We do **not** vendor or republish it: its from-source libvips build is
heavy (and the original vendored Dockerfile failed to build in CI — `meson` was
missing). The upstream image is `linux/amd64` and already battle-tested, so there
is little value in re-hosting it. It is therefore excluded from
`build-images.yml`. If you ever want a self-hosted copy, add a `containers/valis/`
Dockerfile that installs `meson`/`ninja` before the libvips build and re-add
`valis` to the build matrix.

All published images are built for **`linux/amd64`** (the pipeline's HPC target).

## Orphaned / legacy contexts NOT vendored

The external `Docker images/` working directory contained additional build
contexts that are **not** part of the current Mirage pipeline. They were
intentionally **not** vendored because no pipeline process references their
image tag, and bundling them would bloat the repo and the release build matrix.
They remain available in the author's working tree if ever needed again.

| Legacy context | Why it was not vendored |
| --- | --- |
| `R` | R/Bioconductor scratch image; no Mirage process uses it. |
| `conversion` | Superseded by the `bioformats` context for `CONVERT_IMAGE`. |
| `convert_bioformats` | Earlier conversion image; superseded by `bioformats` (`convert_bioformats_2` tag is built from the `bioformats` context). |
| `convert_to_tiff` | One-off TIFF conversion experiment; not wired into any module. |
| `copy` | Trivial passthrough/utility image; not referenced. |
| `deep_cell_types` | DeepCell cell-typing experiment; not part of the current workflow. |
| `diffeo` | Predecessor of `debug_diffeo`; the pipeline uses `debug_diffeo` for `GENERATE_REGISTRATION_QC`. |
| `fastmorph` | Morphology experiment; no module references it. |
| `jupyter` | Interactive notebook image for local exploration; not a pipeline runtime. |
| `pixie` | Pixie clustering experiment; not part of the current pipeline (a stray `containers/pixie/Dockerfile` may exist locally but is not built or published by the release workflow). |

## `container` directives

The pipeline modules (`.nf` files) reference the Docker Hub tags in the mapping
table above directly (e.g. `container 'bolt3x/attend_image_analysis:preprocess'`).
Tags are fixed and content-descriptive — pin them explicitly, never `:latest`,
so runs stay reproducible.

`modules/local/segment.nf` is the one dynamic case: `SEGMENT` picks its container
at runtime from `params.seg_method` (`cellsam` → `:cellsam`, `instantseg` →
`:instant_seg`, otherwise the StarDist default `:segmentation_gpu`).

The distributed non-rigid tiling modules pull the patched VALIS image via
`params.reg_dist_container` (default `bolt3x/attend_image_analysis:mirage_valis_1.0.0`),
published by [`build-valis-image.yml`](../.github/workflows/build-valis-image.yml).
`REGISTER` / `ESTIMATE_FEATURE_DISTANCES` use the upstream `cdgatenbee/valis-wsi:1.0.0`.
