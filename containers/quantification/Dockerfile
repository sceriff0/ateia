
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
    cupy-cuda12x \
    cucim \
    numpy \
    pandas \
    scikit-image \
    scipy \
    matplotlib \
    tqdm \
    psutil \
    aicsimageio \
    aicsimageio[all] \
    tifffile
