# DIGEST-PINNED (ruling R6). The tag stays for readability; the digest is what makes the
# build reproducible -- a version tag is republished, so rebuilding from the tag alone can
# produce a different image than the one that generated the published results. Resolved
# 2026-09-02; Dependabot updates the digest in place. Guarded by
# tests/test_base_images_are_digest_pinned.py.
FROM tensorflow/tensorflow:2.15.0-gpu-jupyter@sha256:2de4ac3c6e8360a1e57b7dc4fca8d061daf7c58b61de31da1f0aca11c18bab32

WORKDIR /workspace

# Install system dependencies first (if needed)
ARG NVIDIA_DRIVER_VERSION=470

# procps is not optional. Nextflow's task-metrics wrapper hard-exits BEFORE the script
# block when `ps` is absent, and params.enable_trace defaults to true, so every task in
# this image would fail with exit status 1 and empty stdout. Installed in all eleven
# images rather than "verified present in this base" -- apt is a no-op where it already
# is, and containers/<dir>/smoke.sh asserts it either way.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ocl-icd-dev \
    ocl-icd-opencl-dev \
    opencl-headers \
    clinfo \
    libnvidia-compute-${NVIDIA_DRIVER_VERSION} \
    procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages with exact versions

# The build context is the REPOSITORY ROOT (containers.yml passes `context: .`), so the
# harmonised pins come from requirements/constraints.txt -- the SAME file the workflows and
# the other images read. The inline pins below must AGREE with it: a constraint does not
# override a divergent direct requirement, it conflicts with it and fails the build, which
# is exactly the behaviour wanted.
COPY requirements/constraints.txt /tmp/requirements/constraints.txt
#
# csbdeep and dask are named explicitly even though stardist already pulls csbdeep in
# transitively: segment.py imports both directly (`from csbdeep.utils import normalize`,
# `import dask.array as da` for the map_overlap-based tiled read), and an install nothing
# in this Dockerfile names is invisible to tests/test_container_harmonisation.py's
# "installs what it imports" guard -- and to anyone reading this file to see what the
# script actually needs.
#
# csbdeep is ALSO the reason this line is now the pin's only home: containers/regqc
# installed csbdeep too until 2026-09-02, so it lived in requirements/constraints.txt.
# With one installer left, constraints.txt is the wrong place (it is for SHARED packages)
# and the pin lives here.
RUN pip install --no-cache-dir -c /tmp/requirements/constraints.txt \
    stardist==0.9.1 \
    csbdeep==0.8.2 \
    dask==2026.7.1 \
    pandas==2.3.3 \
    numpy==1.26.4 \
    scikit-image==0.25.2 \
    scipy==1.15.3 \
    tifffile==2025.5.10 \
    imagecodecs==2025.3.30 \
    zarr==2.18.3 \
    numcodecs==0.13.1

RUN pip install -c /tmp/requirements/constraints.txt gputools edt

# Re-assert the exact pin: gputools/edt above can pull a newer numpy transitively.
RUN pip install --no-cache-dir -c /tmp/requirements/constraints.txt "numpy==1.26.4"

# Fail the BUILD rather than a cluster run. containers/<dir>/smoke.sh imports exactly
# the third-party modules this image's bin/ scripts import at run time, so a stack that
# resolved in pip and then explodes on first import stops here. The SAME script is run
# again by .github/workflows/containers.yml against the built image, so there is one
# definition exercised in two places rather than two copies that drift.
#
# LAST, and after every pip install, on purpose: a COPY invalidates the layer cache for
# everything below it, so putting this at the end costs no rebuilds.
COPY containers/stardist/smoke.sh /usr/local/bin/mirage-smoke.sh
RUN bash /usr/local/bin/mirage-smoke.sh
