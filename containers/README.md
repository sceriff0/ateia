# Mirage Container Images

This directory vendors the Dockerfiles for every image the Mirage pipeline runs.

**The build context is the REPOSITORY ROOT, not `containers/<name>/`.**
`.github/workflows/containers.yml` passes `context: .` with
`file: containers/<name>/Dockerfile`, because every image takes its Python pins from
[`requirements/`](../requirements) — `constraints.txt` is the one version authority shared
by all eleven images AND by the CI workflows, and a per-directory context cannot reach
outside itself. A local build must therefore be run from the repository root:

```sh
docker build -f containers/segeval/Dockerfile -t bolt3x/mirage-segeval:1.0.0 .
```

Two images take no constraints file and say so in their own header:
`containers/spatialdata` (the python:3.12 / `zarr>=3` island) and `containers/convert`
(bioio caps `tifffile` and forces `numpy` 2.x). Both divergences are recorded in
`tests/test_container_harmonisation.py`'s `PINNED_EXCEPTIONS`, and
`tests/test_ci_stack_pinned.py` derives its exception list from that table rather than
from a hand-written name list.

**How a container's requirements are checked.** `tests/test_container_harmonisation.py`'s
`test_container_installs_what_its_scripts_import` walks each image's own `bin/` scripts
(and the local modules they import) with `ast`, at MODULE SCOPE only — an `import` nested
inside a `def` executes only if something calls it, so it is a runtime dependency, not a
module one, and the walker does not report it. A `class` body is excluded too, for
symmetry rather than the same reason: it DOES execute at import time, but no script in
`bin/` imports anything inside a class body, so the exclusion costs nothing. A container
whose scripts reach a package ONLY through such a lazy import declares it explicitly in that file's
`REQUIRED_RUNTIME_IMPORTS` table (`{container: {import_name: reason}}`), read alongside the
walker's own module-scope findings before the installed-package check runs. Each entry is
paired with `test_required_runtime_imports_are_actually_reached`, which walks the container's
scripts unrestricted (`ast.walk`) to prove the lazy import still exists somewhere reachable —
an entry whose import has since been removed fails there rather than silently letting the
container drop a dependency nothing in it needs any more.

Images are built and published to **Docker Hub**, one public repository per image,
tagged with the pipeline version:

```
bolt3x/mirage-<component>:<manifest.version>
```

The build-context directory, the Docker Hub repository and the `container` directive in
`modules/local/*.nf` all use the same component name, so there is nothing to translate
between them. `tests/test_container_image_naming.py` asserts that.

Publishing is automated by [`.github/workflows/containers.yml`](../.github/workflows/containers.yml)
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
gh workflow run containers.yml --ref <branch> -f version=<tag> -f only=segeval
```

> `<tag>` is a content-descriptive tag (e.g. `preprocess`,
> `convert_bioformats_2`, `tiled`) — not a release version. The modules pin
> these tags directly and never use `:latest`.

> The tag is the pipeline version from `manifest.version`, and the modules pin it
> directly. `:latest` is never used.
>
> This replaces an earlier layout that used a single repository with one *content-descriptive*
> tag per image (`:preprocess`, `:convert_bioformats_2`, …). That put component names and real
> versions (`v2.3`, `v2.4`) in the same namespace, and — because the build workflow resolved a
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
| `preprocess` | `bolt3x/mirage-preprocess:1.0.0` | `TILE_FOR_BASIC`, `APPLY_PROFILES`, `SPLIT_CHANNELS`, `GENERATE_PREPROCESS_QC`, `GENERATE_QC_REPORT`, `PREFLIGHT_SCALE`, `AGGREGATE_SIZE_LOGS` (7 modules) | `ubuntu:22.04` + Python 3.11 + BaSiCPy/JAX(cpu)/scikit-image illumination-correction stack |
| `quantify` | `bolt3x/mirage-quantify:1.0.0` | `QUANTIFY`, `EXTRACT_CELL_PROPERTIES`, `EXTRACT_NUCLEI_PROPERTIES`, `EXPORT_GEOJSON`, `GENERATE_POSTPROCESSING_QC` (+ `quantify.nf` second container directive) (6 modules) | `nvidia/cuda:12.2.2-devel-ubuntu22.04` + numpy/scipy/scikit-image quantification stack |
| `stardist` | `bolt3x/mirage-stardist:1.0.0` | `SEGMENT` (default backend, `params.seg_method` = stardist) | `tensorflow/tensorflow:2.15.0-gpu-jupyter` + StarDist 0.9.1 |
| `cellsam` | `bolt3x/mirage-cellsam:1.0.0` | `SEGMENT` (`params.seg_method` = `cellsam`) | `pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime` + `cellSAM` (git) |
| `instanseg` | `bolt3x/mirage-instanseg:1.0.0` | `SEGMENT` (`params.seg_method` = `instantseg`) | `pytorch/pytorch:2.5.1-cuda11.8-cudnn9-runtime` + `instanseg-torch` |
| `merge` | `bolt3x/mirage-merge:1.0.0` | `MERGE_AND_PYRAMID` | `pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime` + tifffile/imagecodecs pyramid stack |
| `regqc` | `bolt3x/mirage-regqc:1.0.0` | `GENERATE_REGISTRATION_QC` | `nvidia/cuda:12.2.2-cudnn8-devel-ubuntu22.04` + opencv/tifffile/scikit-image/zarr registration-QC stack |
| `tiled` | `bolt3x/mirage-tiled:1.0.0` | `TILED_COARSE`, `TILED_REG_TILE`, `TILED_SOLVE`, `TILED_STITCH`, `WARP_SEG_QC` (tiled backend, STARE `registration_method='tiled'`) | `python:3.11-slim` + numpy/scipy/scikit-image/tifffile/zarr + `torch` 2.3.1 (**CPU wheel**) and `kornia` 0.7.3 for the DISK+LightGlue COARSE front-end, with **both pretrained checkpoints baked in** under `TORCH_HOME=/opt/torch` (`depth-save.pth`, `disk_lightglue_v0-1_arxiv-pth`) so a read-only-`$HOME` / air-gapped cluster never has to download them — **still no JVM/BioFormats/libvips/CUDA runtime**. Carrying torch here retires the earlier "lean, torch-free" claim on purpose: it replaces the separate `stare-ml` image, which was built but never pushed and so failed on image pull for everyone who did not manually dispatch its build. |
| `spatialdata` | `bolt3x/mirage-spatialdata:1.0.0` | `EXPORT_SPATIALDATA` (+ the out-of-band `bin/join_flowpath.py` cohort join) | `python:3.12-slim` + spatialdata/anndata/geopandas/zarr 3 — CPU only, no JVM/GPU |
| `segeval` | `bolt3x/mirage-segeval:1.0.0` | `SEG_QUALITY_EVAL`, `MERGE_SEG_EVAL` (opt-in, `-params-file params/seg_quality_eval.json`) | `python:3.11-slim` + numpy/scipy/pandas/scikit-image/scikit-learn/aicsimageio/tifffile/xmltodict (vendored CSE 1.5.19 subset under `bin/utils/cse/`) |
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
`containers.yml`. If you ever want a self-hosted copy, add a `containers/valis/`
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
