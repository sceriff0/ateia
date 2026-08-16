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
RUN pip install --no-cache-dir \
    stardist==0.9.1 \
    pandas==2.3.3 \
    numpy==1.26.4 \
    scikit-image==0.25.2 \
    scipy==1.15.3 \
    tifffile==2025.5.10 \
    imagecodecs==2025.3.30 \
    zarr==2.18.3 \
    numcodecs==0.13.1

RUN pip install gputools edt

# Re-assert the exact pin: gputools/edt above can pull a newer numpy transitively.
RUN pip install --no-cache-dir "numpy==1.26.4"
