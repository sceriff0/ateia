# Mirage Container Images

This directory vendors the Docker build contexts for every image the Mirage
pipeline runs. Each subdirectory is a self-contained build context
(`containers/<name>/Dockerfile` plus any `requirements.txt` / helper files).

Images are built and published to **Docker Hub**, one public repository per image,
tagged with the pipeline version:

```
bolt3x/mirage-<component>:<manifest.version>
```

The build-context directory, the Docker Hub repository and the `container` directive in
`modules/local/*.nf` all use the same component name, so there is nothing to translate
between them. `tests/test_container_image_naming.py` asserts that.

Publishing is automated by [`.github/workflows/build-images.yml`](../.github/workflows/build-images.yml)
and the release flow in [`.github/workflows/release.yml`](../.github/workflows/release.yml).
CI authenticates with the `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` repository
secrets. Docker Hub images are **public**, so no pull credentials are needed on
the HPC/cluster side (unlike GHCR, whose default-private packages caused
`403 Forbidden` on `singularity pull`).

**`containers/images.json` is the build list.** The GitHub Actions matrix and the
workflow's `only` filter both read it, so adding a directory here without adding a
row there means the image is never built or pushed — the run goes green and the tag
simply does not exist on Docker Hub. That is how `:tiled` shipped unpublished.
`tests/test_container_build_matrix.py` fails on the mismatch in both directions,
and additionally on any `mirage-<dir>:<tag>` a module pulls that the matrix does
not publish.

To rebuild ONE image without republishing the other ten:

```sh
gh workflow run build-images.yml --ref <branch> -f version=<tag> -f only=segeval
```

> `<tag>` is a content-descriptive tag (e.g. `preprocess`,
> `convert_bioformats_2`, `tiled`) — not a release version. The modules pin
> these tags directly and never use `:latest`.

> The tag is the pipeline version from `manifest.version`, and the modules pin it
> directly. `:latest` is never used.
>
> This replaces an earlier layout that used a single repository with one *content-descriptive*
> tag per image (`:preprocess`, `:convert_bioformats_2`, …). That put component names and real
> versions (`v2.3`, `v2.4`) in the same namespace, and — because `build-images.yml` resolved a
> `version` input it then never applied to the tag — every publish overwrote one mutable name,
> leaving no earlier image to roll back to.
>
> **Remaining reproducibility caveat:** a version tag is still mutable if it is re-pushed. For a
> hard guarantee, pin by immutable digest (`@sha256:…`). The version tag makes rollback
> *possible*; only a digest makes a checkout byte-for-byte reproducible.

## Image mapping

| Build context (`containers/<name>/`) | Docker Hub image:tag | Pipeline process(es) that use it | Source / base image |
| --- | --- | --- | --- |
| `convert` | `bolt3x/mirage-convert:1.0.0` | `CONVERT_IMAGE` | `eclipse-temurin:21-jre-jammy` + Glencoe `bioformats2raw` 0.12.0 / `raw2ometiff` 0.10.0 + `tifffile`/`numpy` |
| `preprocess` | `bolt3x/mirage-preprocess:1.0.0` | `PREPROCESS`, `GENERATE_PREPROCESS_QC`, `GENERATE_QC_REPORT`, `SPLIT_CHANNELS` (4 modules) | `ubuntu:22.04` + Python 3.11 + BaSiCPy/JAX(cpu)/scikit-image illumination-correction stack |
| `quantify` | `bolt3x/mirage-quantify:1.0.0` | `QUANTIFY`, `EXTRACT_CELL_PROPERTIES`, `EXTRACT_NUCLEI_PROPERTIES`, `EXPORT_GEOJSON`, `GENERATE_POSTPROCESSING_QC` (+ `quantify.nf` second container directive) (6 modules) | `nvidia/cuda:12.2.2-devel-ubuntu22.04` + numpy/scipy/scikit-image quantification stack |
| `stardist` | `bolt3x/mirage-stardist:1.0.0` | `SEGMENT` (default backend, `params.seg_method` = stardist) | `tensorflow/tensorflow:2.15.0-gpu-jupyter` + StarDist 0.9.1 |
| `cellsam` | `bolt3x/mirage-cellsam:1.0.0` | `SEGMENT` (`params.seg_method` = `cellsam`) | `pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime` + `cellSAM` (git) |
| `instanseg` | `bolt3x/mirage-instanseg:1.0.0` | `SEGMENT` (`params.seg_method` = `instantseg`) | `pytorch/pytorch:2.5.1-cuda11.8-cudnn9-runtime` + `instanseg-torch` |
| `merge` | `bolt3x/mirage-merge:1.0.0` | `MERGE_AND_PYRAMID` | `pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime` + tifffile/imagecodecs pyramid stack |
| `regqc` | `bolt3x/mirage-regqc:1.0.0` | `GENERATE_REGISTRATION_QC` | `nvidia/cuda:12.2.2-cudnn8-devel-ubuntu22.04` + Miniconda/bftools + StarDist/cudipy diffeo QC stack |
| `tiled` | `bolt3x/mirage-tiled:1.0.0` | `TILED_REGISTER`, `WARP_SEG_QC` (tiled backend, STARE `registration_method='tiled'`) | `python:3.11-slim` + numpy/scipy/scikit-image/tifffile — **no JVM/BioFormats/libvips/GPU** (~438 MB, vs the multi-GB VALIS image) |
| `stare-ml` | `bolt3x/mirage-stare-ml:1.0.0` | `TILED_COARSE` ONLY, and only under `-profile stare_ml` (STARE's learned global-alignment front-end, `--reg_tiled_frontend disk_lightglue`) | `python:3.11-slim` + the same CPU stack as `tiled` (incl. `zarr`/`numcodecs`/`imagecodecs`, since `TILED_COARSE` transitively needs them) + `torch` (CPU wheel) + `kornia` for DISK+LightGlue — a SECOND, OPTIONAL image so the default `tiled` image stays JVM/GPU/torch-free; see the Dockerfile header. **NOT YET IMPLEMENTED**: the DISK+LightGlue matching body is a TODO (`bin/utils/coarse_align.py::_frontend_disk_lightglue`) — selecting `disk_lightglue` always raises `NotImplementedError` today, even under this profile. **Not yet published**: like `:tiled` before it, a plain push to `main` builds this image but does not push it — run `gh workflow run build-images.yml -f version=1.0.0 -f only=stare-ml` before the first `-profile stare_ml` run, or the image pull fails. |
| `spatialdata` | `bolt3x/mirage-spatialdata:1.0.0` | `EXPORT_SPATIALDATA` (+ the out-of-band `bin/join_flowpath.py` cohort join) | `python:3.12-slim` + spatialdata/anndata/geopandas/zarr 3 — CPU only, no JVM/GPU |
| `segeval` | `bolt3x/mirage-segeval:${params.segeval_tag}` | `SEG_QUALITY_EVAL`, `MERGE_SEG_EVAL` (opt-in, `-params-file params/seg_quality_eval.json`) | `python:3.11-slim` + numpy/scipy/pandas/scikit-image/scikit-learn/aicsimageio/tifffile/xmltodict (vendored CSE 1.5.19 subset under `bin/utils/cse/`) — the one image whose tag stays a parameter, because it is opt-in and published separately |
| VALIS (not vendored) | `cdgatenbee/valis-wsi:1.0.0` (upstream) | `REGISTER` | upstream maintained image — **not rebuilt or published by us** (see note below) |

> **zarr major versions differ on purpose.** `spatialdata` pins `zarr>=3.0.0`
> (required by `spatialdata>=0.8.0`, which writes NGFF `"0.5-dev-spatialdata"`),
> while `tiled` pins `zarr==2.18.3` for `tifffile`'s `aszarr` region reads. Keeping
> them in separate images is what lets both constraints hold without either being
> downgraded.

> The historical typo'd context directory `istantseg` was renamed to `instanseg` as part
> of the one-repo-per-image rename, so the build-context directory, the Docker Hub
> repository and the `container` directive all now agree on the spelling. The published
> image is `bolt3x/mirage-instanseg:1.0.0` on **Docker Hub** (not GHCR — see "Publishing"
> above), even though `params.seg_method` is `instantseg`.

### VALIS — uses the upstream image (not vendored)

`REGISTER` references the maintained upstream image [`cdgatenbee/valis-wsi:1.0.0`](https://hub.docker.com/r/cdgatenbee/valis-wsi)
directly. We do **not** vendor or republish it: its from-source libvips build is
heavy (and the original vendored Dockerfile failed to build in CI — `meson` was
missing). The upstream image is `linux/amd64` and already battle-tested, so there
is little value in re-hosting it. It is therefore excluded from
`build-images.yml`. If you ever want a self-hosted copy, add a `containers/valis/`
Dockerfile that installs `meson`/`ninja` before the libvips build and re-add
`valis` to the build matrix.

All published images are built for **`linux/amd64`** (the pipeline's HPC target).

## Runtime requirements every image must satisfy

**`procps` (i.e. `ps`) is mandatory in every image**, regardless of what the process
actually runs. `params.enable_trace` defaults to `true` (`nextflow.config`), so Nextflow
injects its task-metrics wrapper into *every* task, and that wrapper begins with a hard
guard (`nextflow/executor/command-trace.txt`):

```bash
command -v ps &>/dev/null || { >&2 echo "Command 'ps' required by nextflow to collect task metrics cannot be found"; exit 1; }
```

It runs **before** the process's `script:` block, so a missing `ps` fails the task with
**exit status 1, empty stdout and no traceback** — the failure looks like the tool
crashed silently, not like a missing system package. Debian/Ubuntu bases do **not** ship
`procps`: `python:*-slim` and `ubuntu:22.04` both lack it, which is why `preprocess`,
`spatialdata` and `tiled` install it explicitly. Bases derived from the CUDA/PyTorch/
TensorFlow images happen to carry it already.

When adding a new build context, either install `procps` or verify the base has it, and
add a build-time assertion so a regression fails the **build** rather than a cluster run:

```dockerfile
RUN ps -e -o pid= -o ppid= > /dev/null && echo "procps OK"
```


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
| `diffeo` | Predecessor of `debug_diffeo`, itself superseded by the vendored `regqc` context (`bolt3x/mirage-regqc:1.0.0`), which the pipeline now uses for `GENERATE_REGISTRATION_QC`. |
| `fastmorph` | Morphology experiment; no module references it. |
| `jupyter` | Interactive notebook image for local exploration; not a pipeline runtime. |
| `pixie` | Pixie clustering experiment; not part of the current pipeline (a stray `containers/pixie/Dockerfile` may exist locally but is not built or published by the release workflow). |

## `container` directives

The pipeline modules (`.nf` files) reference the Docker Hub tags in the mapping
table above directly (e.g. `container 'bolt3x/mirage-preprocess:1.0.0'`).
The image NAME is content-descriptive (one repository per image); the TAG is a
fixed, immutable SemVer version tied to `manifest.version` — pin it explicitly,
never `:latest`, so runs stay reproducible.

`modules/local/segment.nf` is the one dynamic case: `SEGMENT` picks its container at runtime
from `params.seg_method`, via the table in `lib/SegBackends.groovy` (`cellsam` →
`mirage-cellsam`, `instantseg` → `mirage-instanseg`, otherwise the StarDist default
`mirage-stardist`).

`REGISTER` uses the upstream `cdgatenbee/valis-wsi:1.0.0` directly; VALIS is deliberately not
re-hosted, because its from-source libvips build is heavy and upstream maintains it.
