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
    pandas==2.2.3 \
    numpy==1.26.4 \
    scikit-image==0.25.2 \
    aicsimageio==4.14.0

RUN pip install gputools edt

RUN pip install --no-cache-dir "numpy<2.0"
