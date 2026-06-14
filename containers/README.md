# Mirage Container Images

This directory vendors the Docker build contexts for every image the Mirage
pipeline runs. Each subdirectory is a self-contained build context
(`containers/<name>/Dockerfile` plus any `requirements.txt` / helper files).

Images are built and published to the **GitHub Container Registry (GHCR)** at:

```
ghcr.io/sceriff0/mirage/<name>:<tag>
```

Publishing is automated by [`.github/workflows/build-images.yml`](../.github/workflows/build-images.yml)
and the release flow in [`.github/workflows/release.yml`](../.github/workflows/release.yml).
Every image is tagged with both the release version (e.g. `v1.0.0`) and `latest`.

> `<tag>` below is the pipeline release version, e.g. `v1.0.0`. We avoid
> `:latest` in the pipeline's `container` directives so runs are reproducible.

## Image mapping

| Build context (`containers/<name>/`) | GHCR image:tag | Pipeline process(es) that use it | Source / base image |
| --- | --- | --- | --- |
| `bioformats` | `ghcr.io/sceriff0/mirage/bioformats:<tag>` | `CONVERT_IMAGE` | `eclipse-temurin:21-jre-jammy` + Glencoe `bioformats2raw` 0.12.0 / `raw2ometiff` 0.10.0 + `tifffile`/`numpy` |
| `preprocess` | `ghcr.io/sceriff0/mirage/preprocess:<tag>` | `PREPROCESS`, `GENERATE_PREPROCESS_QC`, `GENERATE_QC_REPORT`, `GET_IMAGE_DIMS`, `MAX_DIM`, `PAD_IMAGES`, `SPLIT_CHANNELS` (7 modules) | `ubuntu:22.04` + Python 3.11 + BaSiCPy/JAX(cpu)/scikit-image illumination-correction stack |
| `quantification` | `ghcr.io/sceriff0/mirage/quantification:<tag>` | `QUANTIFY`, `EXTRACT_CELL_PROPERTIES`, `EXTRACT_NUCLEI_PROPERTIES`, `EXPORT_GEOJSON`, `GENERATE_POSTPROCESSING_QC` (+ `quantify.nf` second container directive) (6 modules) | `nvidia/cuda:12.2.2-devel-ubuntu22.04` + cupy/cucim GPU quantification stack |
| `segmentation` | `ghcr.io/sceriff0/mirage/segmentation:<tag>` | `SEGMENT` (default backend, `params.seg_method` = stardist) | `tensorflow/tensorflow:2.15.0-gpu-jupyter` + StarDist 0.9.1 / Cellpose 3.1.1.1 |
| `cellsam` | `ghcr.io/sceriff0/mirage/cellsam:<tag>` | `SEGMENT` (`params.seg_method` = `cellsam`) | `pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime` + `cellSAM` (git) |
| `istantseg` | `ghcr.io/sceriff0/mirage/istantseg:<tag>` | `SEGMENT` (`params.seg_method` = `instantseg`) | `pytorch/pytorch:2.5.1-cuda11.8-cudnn9-runtime` + `instanseg-torch` |
| `merge` | `ghcr.io/sceriff0/mirage/merge:<tag>` | `MERGE_AND_PYRAMID` | `pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime` + tifffile/imagecodecs pyramid stack |
| `debug_diffeo` | `ghcr.io/sceriff0/mirage/debug_diffeo:<tag>` | `GENERATE_REGISTRATION_QC` | `nvidia/cuda:12.2.2-cudnn8-devel-ubuntu22.04` + Miniconda/bftools + StarDist/cudipy diffeo QC stack |
| VALIS (not vendored) | `cdgatenbee/valis-wsi:1.0.0` (upstream) | `REGISTER`, `ESTIMATE_FEATURE_DISTANCES` | upstream maintained image — **not rebuilt or published by us** (see note below) |

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

## Recommended `container` directive replacements

The pipeline modules (`.nf` files) currently reference the legacy DockerHub
tags. To switch to the GHCR-published images, replace the `container` directives
as follows. `${version}` is the release tag (e.g. `v1.0.0`); pin it explicitly
rather than using `:latest`.

> These edits are owned by the modules/config agent — this list is a
> recommendation, not applied here.

| File | Line | Old directive | Recommended new directive |
| --- | --- | --- | --- |
| `modules/local/convert_image.nf` | 5 | `container 'bolt3x/attend_image_analysis:convert_bioformats_2'` | `container 'ghcr.io/sceriff0/mirage/bioformats:${version}'` |
| `modules/local/preprocess.nf` | 14 | `container 'bolt3x/attend_image_analysis:preprocess'` | `container 'ghcr.io/sceriff0/mirage/preprocess:${version}'` |
| `modules/local/generate_preprocess_qc.nf` | 5 | `container 'bolt3x/attend_image_analysis:preprocess'` | `container 'ghcr.io/sceriff0/mirage/preprocess:${version}'` |
| `modules/local/generate_qc_report.nf` | 5 | `container 'bolt3x/attend_image_analysis:preprocess'` | `container 'ghcr.io/sceriff0/mirage/preprocess:${version}'` |
| `modules/local/get_image_dims.nf` | 5 | `container 'bolt3x/attend_image_analysis:preprocess'` | `container 'ghcr.io/sceriff0/mirage/preprocess:${version}'` |
| `modules/local/max_dim.nf` | 5 | `container 'bolt3x/attend_image_analysis:preprocess'` | `container 'ghcr.io/sceriff0/mirage/preprocess:${version}'` |
| `modules/local/pad_images.nf` | 5 | `container 'bolt3x/attend_image_analysis:preprocess'` | `container 'ghcr.io/sceriff0/mirage/preprocess:${version}'` |
| `modules/local/split_channels.nf` | 14 | `container 'bolt3x/attend_image_analysis:preprocess'` | `container 'ghcr.io/sceriff0/mirage/preprocess:${version}'` |
| `modules/local/quantify.nf` | 14 | `container 'bolt3x/attend_image_analysis:quantification_gpu'` | `container 'ghcr.io/sceriff0/mirage/quantification:${version}'` |
| `modules/local/quantify.nf` | 81 | `container 'bolt3x/attend_image_analysis:quantification_gpu'` | `container 'ghcr.io/sceriff0/mirage/quantification:${version}'` |
| `modules/local/extract_cell_properties.nf` | 18 | `container 'bolt3x/attend_image_analysis:quantification_gpu'` | `container 'ghcr.io/sceriff0/mirage/quantification:${version}'` |
| `modules/local/extract_nuclei_properties.nf` | 16 | `container 'bolt3x/attend_image_analysis:quantification_gpu'` | `container 'ghcr.io/sceriff0/mirage/quantification:${version}'` |
| `modules/local/export_geojson.nf` | 19 | `container 'bolt3x/attend_image_analysis:quantification_gpu'` | `container 'ghcr.io/sceriff0/mirage/quantification:${version}'` |
| `modules/local/generate_postprocessing_qc.nf` | 5 | `container 'bolt3x/attend_image_analysis:quantification_gpu'` | `container 'ghcr.io/sceriff0/mirage/quantification:${version}'` |
| `modules/local/merge_and_pyramid.nf` | 21 | `container 'bolt3x/attend_image_analysis:merge'` | `container 'ghcr.io/sceriff0/mirage/merge:${version}'` |
| `modules/local/generate_registration_qc.nf` | 5 | `container 'bolt3x/attend_image_analysis:debug_diffeo'` | `container 'ghcr.io/sceriff0/mirage/debug_diffeo:${version}'` |
| `modules/local/register.nf` | 17 | `container 'cdgatenbee/valis-wsi:1.0.0'` | `container 'ghcr.io/sceriff0/mirage/valis:${version}'` |
| `modules/local/estimate_feature_distances.nf` | 4 | `container 'cdgatenbee/valis-wsi:1.0.0'` | `container 'ghcr.io/sceriff0/mirage/valis:${version}'` |

### `modules/local/segment.nf` (dynamic selector, lines 25-29)

`SEGMENT` chooses its container at runtime from `params.seg_method`. Replace the
three tags inside the `container { ... }` closure:

| Old tag (in selector) | Recommended new tag |
| --- | --- |
| `bolt3x/attend_image_analysis:cellsam` | `ghcr.io/sceriff0/mirage/cellsam:${version}` |
| `bolt3x/attend_image_analysis:instant_seg` | `ghcr.io/sceriff0/mirage/istantseg:${version}` |
| `bolt3x/attend_image_analysis:segmentation_gpu` (default) | `ghcr.io/sceriff0/mirage/segmentation:${version}` |

Recommended closure shape:

```groovy
container { params.seg_method == 'cellsam'
            ? 'ghcr.io/sceriff0/mirage/cellsam:${version}'
            : params.seg_method == 'instantseg'
            ? 'ghcr.io/sceriff0/mirage/istantseg:${version}'
            : 'ghcr.io/sceriff0/mirage/segmentation:${version}' }
```

> Because `${version}` interpolates the pipeline release, consider centralizing
> it (e.g. a `params.container_registry` / `params.container_tag`) so all
> directives update from a single source. That decision belongs to the
> modules/config agent.
