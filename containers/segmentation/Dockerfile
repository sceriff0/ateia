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
#
# tifffile was previously unpinned here -- resolved implicitly by
# aicsimageio==4.14.0's OWN pin, which is a CEILING, not a floor:
# aicsimageio 4.14.0 requires `tifffile<2023.3.15,>=2021.8.30` (checked
# against PyPI's published metadata for that exact release). That rules out
# every other tifffile version this codebase converges on elsewhere
# (2023.4.12, 2023.7.10, 2024.7.x are all >= the ceiling), so this image
# genuinely cannot join that convergence without removing aicsimageio --
# which Task 11b did NOT do here (only containers/quantification's
# aicsimageio was in scope; a full bin/ grep shows aicsimageio is in fact
# also unused by segment.py, the only script this image runs, but pulling
# it is a separate change -- see docs/containers.md and the task-11b report's
# "Noticed, not fixed"). tifffile==2023.2.28 is the newest release that
# satisfies aicsimageio's ceiling (verified against PyPI's release list).
RUN pip install --no-cache-dir \
    stardist==0.9.1 \
    pandas==2.2.3 \
    numpy==1.26.4 \
    cellpose==3.1.1.1 \
    scikit-image==0.25.2 \
    aicsimageio==4.14.0 \
    tifffile==2023.2.28

RUN pip install gputools edt

RUN pip install --no-cache-dir "numpy<2.0"
