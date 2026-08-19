
# Option 1: NVIDIA CUDA base (most reliable)
FROM nvidia/cuda:12.2.2-devel-ubuntu22.04

# Install Python and essential tools
RUN apt-get update && apt-get install -y \
    python3.10 \
    python3-pip \
    python3.10-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create python symlink
RUN ln -s /usr/bin/python3.10 /usr/bin/python

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# Install GPU packages
RUN pip install --no-cache-dir \
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
