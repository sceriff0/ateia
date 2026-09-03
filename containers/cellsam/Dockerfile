# torch 2.6.0 is the first release carrying the fix for CVE-2025-32434 (torch.load RCE);
# cellSAM downloads model weights, so that path is reachable. cuda12.1 is not published for
# 2.6.0 -- 12.4 is the nearest tag.
# DIGEST-PINNED (ruling R6). The tag stays for readability; the digest is what makes the
# build reproducible -- a version tag is republished, so rebuilding from the tag alone can
# produce a different image than the one that generated the published results. Resolved
# 2026-09-02; Dependabot updates the digest in place. Guarded by
# tests/test_base_images_are_digest_pinned.py.
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:77f17f843507062875ce8be2a6f76aa6aa3df7f9ef1e31d9d7432f4b0f563dee

# procps is not optional. Nextflow's task-metrics wrapper hard-exits BEFORE the script
# block when `ps` is absent, and params.enable_trace defaults to true, so every task in
# this image would fail with exit status 1 and empty stdout. Installed in all eleven
# images rather than "verified present in this base" -- apt is a no-op where it already
# is, and containers/<dir>/smoke.sh asserts it either way.
RUN apt-get update && apt-get install -y --no-install-recommends git procps \
    && rm -rf /var/lib/apt/lists/*

# The build context is the REPOSITORY ROOT (containers.yml passes `context: .`), so the
# harmonised pins come from requirements/constraints.txt -- the SAME file the workflows and
# the other images read. The inline pins below must AGREE with it: a constraint does not
# override a divergent direct requirement, it conflicts with it and fails the build, which
# is exactly the behaviour wanted.
COPY requirements/constraints.txt /tmp/requirements/constraints.txt

RUN python -m pip install --upgrade pip

# dask and scikit-image are named explicitly even though cellSAM's own dependency chain
# likely pulls scikit-image in transitively: segment_cellsam.py imports both directly
# (`import dask.array as da`, `from skimage import segmentation`), and an install nothing
# in this Dockerfile names is invisible to
# tests/test_container_harmonisation.py's "installs what it imports" guard.
#
# pandas was removed 2026-09-02: bin/segment_cellsam.py does not import it. Every install
# in this file passes `-c constraints.txt`, so if cellSAM's own chain pulls pandas it is
# still held at the harmonised 2.3.3 -- an explicit line only claimed a dependency this
# image's script does not have.
RUN pip install -c /tmp/requirements/constraints.txt \
    numpy==1.26.4 \
    tifffile==2025.5.10 \
    zarr==2.18.3 \
    numcodecs==0.13.1 \
    dask==2026.7.1 \
    scikit-image==0.25.2

RUN python -m pip install -c /tmp/requirements/constraints.txt "imagecodecs==2025.3.30"

# Pinned: an unpinned git install makes every rebuild a different image.
RUN pip install -c /tmp/requirements/constraints.txt git+https://github.com/vanvalenlab/cellSAM.git@362a8e521bcd7313d5cb280bcd4cfc12977c1e9f

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

COPY containers/cellsam/smoke.sh /usr/local/bin/mirage-smoke.sh
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
