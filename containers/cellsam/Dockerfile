# torch 2.6.0 is the first release carrying the fix for CVE-2025-32434 (torch.load RCE);
# cellSAM downloads model weights, so that path is reachable. cuda12.1 is not published for
# 2.6.0 -- 12.4 is the nearest tag.
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

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
RUN pip install -c /tmp/requirements/constraints.txt \
    numpy==1.26.4 \
    pandas==2.3.3 \
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
COPY containers/cellsam/smoke.sh /usr/local/bin/mirage-smoke.sh
RUN bash /usr/local/bin/mirage-smoke.sh
