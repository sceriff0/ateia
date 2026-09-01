# torch 2.6.0 carries the CVE-2025-32434 fix; cuda11.8 is unchanged.
FROM pytorch/pytorch:2.6.0-cuda11.8-cudnn9-runtime

LABEL maintainer="MIRAGE Pipeline"
LABEL description="InstanSeg segmentation container (PyTorch 2.5.1 + CUDA 12.4)"
LABEL version="1.0.0"

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /workspace

# The build context is the REPOSITORY ROOT (build-images.yml passes `context: .`), so the
# harmonised pins come from requirements/constraints.txt -- the SAME file the workflows and
# the other images read. The inline pins below must AGREE with it: a constraint does not
# override a divergent direct requirement, it conflicts with it and fails the build, which
# is exactly the behaviour wanted.
COPY requirements/constraints.txt /tmp/requirements/constraints.txt

# OpenCL ICD loader and headers (kept in case downstream tools need OpenCL;
# unrelated to InstanSeg itself, which uses PyTorch/CUDA).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ocl-icd-dev \
    ocl-icd-opencl-dev \
    opencl-headers \
    clinfo \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -c /tmp/requirements/constraints.txt \
    instanseg-torch==0.1.1 \
    imagecodecs==2025.3.30 \
    numpy==1.26.4 \
    tifffile==2025.5.10 \
    zarr==2.18.3 \
    numcodecs==0.13.1
