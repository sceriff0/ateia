
# Option 1: NVIDIA CUDA base (most reliable)
FROM nvidia/cuda:12.2.2-devel-ubuntu22.04

# Install Python and essential tools
# procps is not optional. Nextflow's task-metrics wrapper hard-exits BEFORE the script
# block when `ps` is absent, and params.enable_trace defaults to true, so every task in
# this image would fail with exit status 1 and empty stdout. Installed in all eleven
# images rather than "verified present in this base" -- apt is a no-op where it already
# is, and containers/<dir>/smoke.sh asserts it either way.
RUN apt-get update && apt-get install -y \
    python3.10 \
    python3-pip \
    python3.10-dev \
    git \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Create python symlink
RUN ln -s /usr/bin/python3.10 /usr/bin/python

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# Install GPU packages

# The build context is the REPOSITORY ROOT (containers.yml passes `context: .`), so the
# harmonised pins come from requirements/constraints.txt -- the SAME file the workflows and
# the other images read. The inline pins below must AGREE with it: a constraint does not
# override a divergent direct requirement, it conflicts with it and fails the build, which
# is exactly the behaviour wanted.
COPY requirements/constraints.txt /tmp/requirements/constraints.txt
RUN pip install --no-cache-dir -c /tmp/requirements/constraints.txt \
    numpy==1.26.4 \
    pandas==2.3.3 \
    scikit-image==0.25.2 \
    scipy==1.15.3 \
    matplotlib==3.9.2 \
    tqdm==4.67.1 \
    psutil==6.1.1 \
    tifffile==2025.5.10 \
    imagecodecs==2025.3.30 \
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
COPY containers/quantify/smoke.sh /usr/local/bin/mirage-smoke.sh
RUN bash /usr/local/bin/mirage-smoke.sh
