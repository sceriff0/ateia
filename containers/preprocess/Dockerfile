FROM ubuntu:22.04

LABEL maintainer="mirage team"
LABEL description="Docker image for mirage image conversion and illumination-correction tiling"
LABEL version="1.0.0"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Install system dependencies including Python 3.11
RUN apt-get update && apt-get install -y \
    software-properties-common \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y \
    python3.11 \
    python3.11-dev \
    python3.11-distutils \
    python3-pip \
    build-essential \
    git \
    ca-certificates \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Set Python 3.11 as default
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1

# Set working directory
WORKDIR /app

# Upgrade pip
RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel

# Install Python dependencies for preprocessing
RUN pip install --no-cache-dir \
    numpy==1.26.4 \
    scipy==1.15.3 \
    tifffile==2025.5.10 \
    imagecodecs==2025.3.30 \
    zarr==2.18.3 \
    numcodecs==0.13.1 \
    scikit-image==0.25.2 \
    nd2 \
    imagecodecs
