FROM pytorch/pytorch:2.5.1-cuda11.8-cudnn9-runtime

LABEL maintainer="MIRAGE Pipeline"
LABEL description="InstanSeg segmentation container (PyTorch 2.5.1 + CUDA 12.4)"
LABEL version="1.0.0"

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /workspace

# OpenCL ICD loader and headers (kept in case downstream tools need OpenCL;
# unrelated to InstanSeg itself, which uses PyTorch/CUDA).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ocl-icd-dev \
    ocl-icd-opencl-dev \
    opencl-headers \
    clinfo \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# tifffile is not a direct dependency of either instanseg-torch or
# imagecodecs' base install (only under their optional extras); it arrives
# transitively via instanseg-torch's unconditional `scikit-image>=0.21.0`,
# which itself only floors tifffile at >=2022.8.12 -- so before Task 11b
# this image had no explicit tifffile pin at all and floated to whatever
# scikit-image's resolver picked on that day's build. Pinned explicitly at
# 2024.7.2 to converge with containers/tiled, containers/merge and
# containers/quantification -- see docs/containers.md.
RUN pip install --no-cache-dir instanseg-torch imagecodecs tifffile==2024.7.2
