FROM ubuntu:22.04

LABEL maintainer="mirage team"
LABEL description="Docker image for BaSiC illumination correction preprocessing"
LABEL version="1.0.0"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    JAX_PLATFORM_NAME=cpu

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

# tifffile==2023.7.10 is left as-is (Task 11b, 2026-08-14): this image also
# pins numpy==1.24.3, scikit-image==0.21.0 and jax[cpu]==0.4.20, an
# already-old, presumably-tested combination. This task's hard constraint
# (no image builds available here) means a version bump can't be verified
# against that specific combination, and PREPROCESS's illumination-corrected
# output feeding every downstream step makes a silent behavioural drift the
# worst place to guess. Left untouched. See docs/containers.md.
# Install Python dependencies for preprocessing
RUN pip install --no-cache-dir \
    numpy==1.24.3 \
    scipy==1.11.4 \
    tifffile==2023.7.10 \
    scikit-image==0.21.0 \
    jax[cpu]==0.4.20 \
    gradient-free-optimizers==1.6.0 \
    hyperactive==4.8.0 \
    basicpy==1.2.0 \
    nd2 \
    imagecodecs
