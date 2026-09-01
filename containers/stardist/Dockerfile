FROM tensorflow/tensorflow:2.15.0-gpu-jupyter

WORKDIR /workspace

# Install system dependencies first (if needed)
ARG NVIDIA_DRIVER_VERSION=470

RUN apt-get update && apt-get install -y --no-install-recommends \
    ocl-icd-dev \
    ocl-icd-opencl-dev \
    opencl-headers \
    clinfo \
    libnvidia-compute-${NVIDIA_DRIVER_VERSION} \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages with exact versions

# The build context is the REPOSITORY ROOT (build-images.yml passes `context: .`), so the
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
