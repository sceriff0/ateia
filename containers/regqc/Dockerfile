# Registration-QC image — bolt3x/mirage-regqc:1.0.0
#
# ONE PROCESS: GENERATE_REGISTRATION_QC (modules/local/generate_registration_qc.nf), whose
# entrypoint is bin/generate_registration_qc.py -> bin/utils/qc.py. That path imports cv2,
# numpy, tifffile, skimage.transform and (via bin/utils/tiled_io.open_lazy) zarr, and
# NOTHING ELSE. containers/regqc/smoke.sh asserts exactly that set.
#
# WHAT WAS REMOVED 2026-09-02, and why none of it was load-bearing:
#   * tensorflow==2.17.1 / stardist==0.9.1 / csbdeep==0.8.2 / gputools / edt — a whole
#     segmentation stack in the QC image. SEGMENT runs in containers/stardist; nothing on
#     this image's runtime path imports any of them. tests/test_no_unreachable_container_
#     frameworks.py could not catch it, because `stardist` IS in seg_method's schema enum
#     and its "selectable" branch therefore skipped the check.
#   * Miniconda + bioconda bftools — the ONLY reason this image carried a second Python.
#     Nothing in bin/, lib/, modules/ or conf/ invokes bfconvert or showinf; the single
#     grep hit is a comment in modules/local/merge_and_pyramid.nf saying the pyramid is
#     written WITHOUT bfconvert. Two interpreters on $PATH is how a pin gets installed
#     into the one the task does not run — see requirements/regqc.txt's own scipy story.
#   * h5py / nd2 / nd2reader / matplotlib / pandas / dask — unimported on this path. dask's
#     comment said it "arrives transitively (nd2 -> resource-backed-dask-array -> dask)",
#     which stops being true when nd2 goes.
#   * ocl-icd-* / opencl-headers / clinfo (OpenCL, for gputools alone), wget (fetched the
#     Miniconda installer), git (unused) and build-essential (every remaining pin ships a
#     manylinux wheel for CPython 3.10).
#
# ffmpeg / libsm6 / libxext6 STAY: requirements/regqc.txt installs the NON-headless
# opencv-python because this image has the display libs, and those are its runtime shared
# libraries. CI installs the headless build of the same release; see
# requirements/constraints.txt's opencv block.
#
# The CUDA base is deliberately unchanged even though nothing here uses CUDA any more.
# Swapping it for python:3.11-slim would also swap opencv's system libraries, which is a
# different change from this one.
#
# DIGEST-PINNED (ruling R6). The tag stays for readability; the digest is what makes the
# build reproducible -- a version tag is republished, so rebuilding from the tag alone can
# produce a different image than the one that generated the published results. Resolved
# 2026-09-02; Dependabot updates the digest in place. Guarded by
# tests/test_base_images_are_digest_pinned.py.
FROM nvidia/cuda:12.2.2-cudnn8-devel-ubuntu22.04@sha256:217134b60289ced62b47463be32584329baf35cb55a9ccda6626b91bd59803be

ENV DEBIAN_FRONTEND=noninteractive

# procps is not optional. Nextflow's task-metrics wrapper hard-exits BEFORE the script
# block when `ps` is absent, and params.enable_trace defaults to true, so every task in
# this image would fail with exit status 1 and empty stdout. Installed in all eleven
# images rather than "verified present in this base" -- apt is a no-op where it already
# is, and containers/<dir>/smoke.sh asserts it either way.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsm6 \
    libxext6 \
    procps \
    python3 \
    python3-pip \
    python3-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV CUDA_HOME=/usr/local/cuda
ENV PATH=${CUDA_HOME}/bin:${PATH}
ENV LD_LIBRARY_PATH=${CUDA_HOME}/lib64:${LD_LIBRARY_PATH}

# The build context is the REPOSITORY ROOT (containers.yml passes `context: .`).
# regqc.txt opens with `-c constraints.txt`, which pip resolves next to it, so both files
# land in the same directory; the inline pins below pass the same constraints file on the
# command line. A pin that drifts from requirements/constraints.txt fails the BUILD.
COPY requirements/constraints.txt requirements/regqc.txt /tmp/requirements/

RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel

# `python3 -m pip`, not a bare `pip`, everywhere in this file: the bare name used to
# resolve to Miniconda's pip rather than the apt interpreter's, which is exactly how a
# pin lands in the Python the task does not run.
#
# scipy is deliberately NOT here. It is pinned in requirements/regqc.txt, which is the LAST
# pip step below -- because a pin in THIS layer is only as good as what the later steps do
# to it, and for the life of this image a later step silently downgraded it (BaSiCPy, now
# removed, capped scipy<1.13). scikit-image pulls scipy anyway and `-c` holds it at the
# harmonised version.
RUN python3 -m pip install --no-cache-dir -c /tmp/requirements/constraints.txt \
    numpy==1.26.4 \
    scikit-image==0.25.2

RUN python3 -m pip install --no-cache-dir -r /tmp/requirements/regqc.txt

# Fail the BUILD rather than a cluster run. containers/<dir>/smoke.sh imports exactly
# the third-party modules this image's bin/ scripts import at run time, so a stack that
# resolved in pip and then explodes on first import stops here. The SAME script is run
# again by .github/workflows/containers.yml against the built image, so there is one
# definition exercised in two places rather than two copies that drift.
#
# LAST, and after every pip install, on purpose: a COPY invalidates the layer cache for
# everything below it, so putting this at the end costs no rebuilds.
# Read-only $HOME safety, the same three variables containers/segeval and
# containers/spatialdata set. Several of these libraries default their caches to ~/.cache;
# nextflow.config's docker profile runs tasks with `-u $(id -u):$(id -g)` and the cluster's
# $HOME is a read-only mount, so a cache write turns into a crash far from its cause -- the
# same class of failure as the VALIS JVM/jgo cache. MPLCONFIGDIR is not decoration here:
# matplotlib writes ~/.config/matplotlib on its first import.
ENV XDG_CACHE_HOME=/tmp/.cache \
    MPLCONFIGDIR=/tmp/.mplconfig \
    NUMBA_CACHE_DIR=/tmp/.numba

COPY containers/regqc/smoke.sh /usr/local/bin/mirage-smoke.sh
RUN bash /usr/local/bin/mirage-smoke.sh

# OCI provenance. LAST IN THE FILE ON PURPOSE: VCS_REF changes on every commit, and an ARG
# referenced earlier would invalidate the layer cache for everything below it on every
# build. Here it invalidates one metadata layer. .github/workflows/containers.yml passes
# both values; a local `docker build` gets "unknown", which is honest.
ARG VCS_REF=unknown
ARG IMAGE_VERSION=unknown
LABEL org.opencontainers.image.source="https://github.com/sceriff0/mirage" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${IMAGE_VERSION}"
