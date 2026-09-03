# DIGEST-PINNED (ruling R6). The tag stays for readability; the digest is what makes the
# build reproducible -- a version tag is republished, so rebuilding from the tag alone can
# produce a different image than the one that generated the published results. Resolved
# 2026-09-02; Dependabot updates the digest in place. Guarded by
# tests/test_base_images_are_digest_pinned.py.
FROM cdgatenbee/valis-wsi:1.0.0@sha256:eac27cc599ae0e54aa01c1bef97538301994ce1abd4da44be3f3130ab85a40e6

# Install required native dependencies for imagecodecs
# procps is not optional. Nextflow's task-metrics wrapper hard-exits BEFORE the script
# block when `ps` is absent, and params.enable_trace defaults to true, so every task in
# this image would fail with exit status 1 and empty stdout. Installed in all eleven
# images rather than "verified present in this base" -- apt is a no-op where it already
# is, and containers/<dir>/smoke.sh asserts it either way.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libjpeg-dev \
        libopenjp2-7-dev \
        libzstd-dev \
        libwebp-dev \
        libtiff-dev \
        libpng-dev \
        liblzma-dev \
        zlib1g-dev \
        procps \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies directly

# The build context is the REPOSITORY ROOT (containers.yml passes `context: .`), so the
# harmonised pins come from requirements/constraints.txt -- the SAME file the workflows and
# the other images read. The inline pins below must AGREE with it: a constraint does not
# override a divergent direct requirement, it conflicts with it and fails the build, which
# is exactly the behaviour wanted.
COPY requirements/constraints.txt /tmp/requirements/constraints.txt
RUN pip install --no-cache-dir -c /tmp/requirements/constraints.txt \
        numpy==1.26.4 \
        numba \
        tifffile==2025.5.10 \
        imagecodecs==2025.3.30 \
        opencv-python==4.10.0.82 \
        zarr==2.18.3 \
        numcodecs==0.13.1

# Fail the BUILD rather than a cluster run. containers/<dir>/smoke.sh imports exactly
# the third-party modules this image's bin/ scripts import at run time, so a stack that
# resolved in pip and then explodes on first import stops here. The SAME script is run
# again by .github/workflows/containers.yml against the built image, so there is one
# definition exercised in two places rather than two copies that drift.
#
# LAST, and after every pip install, on purpose: a COPY invalidates the layer cache for
# everything below it, so putting this at the end costs no rebuilds.
COPY containers/merge/smoke.sh /usr/local/bin/mirage-smoke.sh
RUN bash /usr/local/bin/mirage-smoke.sh
