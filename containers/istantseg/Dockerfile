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

RUN pip install --no-cache-dir instanseg-torch imagecodecs
