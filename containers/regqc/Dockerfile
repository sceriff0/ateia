# Base image: CUDA 12.2.2 + cuDNN8 + Ubuntu 22.04
FROM nvidia/cuda:12.2.2-cudnn8-devel-ubuntu22.04

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# System dependencies
# procps is not optional. Nextflow's task-metrics wrapper hard-exits BEFORE the script
# block when `ps` is absent, and params.enable_trace defaults to true, so every task in
# this image would fail with exit status 1 and empty stdout. Installed in all eleven
# images rather than "verified present in this base" -- apt is a no-op where it already
# is, and containers/<dir>/smoke.sh asserts it either way.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    git \
    ffmpeg \
    libsm6 \
    libxext6 \
    ocl-icd-dev \
    ocl-icd-opencl-dev \
    opencl-headers \
    clinfo \
    python3 \
    python3-pip \
    python3-dev \
    procps \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set environment variables for CUDA
ENV CUDA_HOME=/usr/local/cuda
ENV PATH=${CUDA_HOME}/bin:${PATH}
ENV LD_LIBRARY_PATH=${CUDA_HOME}/lib64:${LD_LIBRARY_PATH}

# ---- Install Miniconda ----
ENV CONDA_DIR=/opt/conda
ENV MINICONDA_HOME=~/miniconda3
ENV PATH=$CONDA_DIR/bin:$PATH

RUN mkdir -p $CONDA_DIR && \
	mkdir -p $MINICONDA_HOME && \
	wget https://repo.anaconda.com/miniconda/Miniconda3-py310_24.9.2-0-Linux-x86_64.sh -O $MINICONDA_HOME/miniconda.sh && \
	bash $MINICONDA_HOME/miniconda.sh -b -u -p $CONDA_DIR && \
	rm $MINICONDA_HOME/miniconda.sh && \
	$CONDA_DIR/bin/conda init bash

# Install bioconda tools (bftools)
RUN conda install -y -c bioconda bftools && conda clean -afy

# ---- Python dependencies ----
# The build context is the REPOSITORY ROOT (containers.yml passes `context: .`).
# regqc.txt opens with `-c constraints.txt`, which pip resolves next to it, so both files
# land in the same directory; the inline pins below pass the same constraints file on the
# command line. A pin that drifts from requirements/constraints.txt fails the BUILD.
COPY requirements/constraints.txt requirements/regqc.txt /tmp/requirements/

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Core scientific + GPU packages.
#
# scipy is deliberately NOT here. It is pinned in requirements/regqc.txt, which is the LAST pip
# step below -- because a pin in THIS layer is only as good as what the later steps do to it, and
# for the life of this image a later step silently downgraded it (BaSiCPy, now removed, capped
# scipy<1.13). scikit-image pulls scipy anyway and `-c` holds it at the harmonised version.
RUN pip install --no-cache-dir -c /tmp/requirements/constraints.txt \
    numpy==1.26.4 \
    pandas==2.3.3 \
    scikit-image==0.25.2 \
    gputools \
    edt

# TensorFlow GPU + Stardist ecosystem
RUN pip install --no-cache-dir -c /tmp/requirements/constraints.txt \
    tensorflow==2.17.1 \
    stardist==0.9.1 \
    csbdeep==0.8.2

# NOTE: a `pip install git+https://github.com/sceriff0/cudipy.git` used to sit here, described
# as "optional GPU image segmentation tools". It was removed 2026-08-31: the repository no
# longer exists (git clone exits 128, "could not resolve to a Repository"), so this image could
# not be built AT ALL -- and nothing in bin/, lib/ or modules/ imports cudipy or dipy. It was
# installed --no-deps, used by nothing, and its absence had gone unnoticed because
# build-images.yml only ran on a push to main touching containers/**. That hole is the
# reason .github/workflows/containers.yml now builds this image nightly as well.

RUN pip install --no-cache-dir -r /tmp/requirements/regqc.txt


# Fail the BUILD rather than a cluster run. containers/<dir>/smoke.sh imports exactly
# the third-party modules this image's bin/ scripts import at run time, so a stack that
# resolved in pip and then explodes on first import stops here. The SAME script is run
# again by .github/workflows/containers.yml against the built image, so there is one
# definition exercised in two places rather than two copies that drift.
#
# LAST, and after every pip install, on purpose: a COPY invalidates the layer cache for
# everything below it, so putting this at the end costs no rebuilds.
COPY containers/regqc/smoke.sh /usr/local/bin/mirage-smoke.sh
RUN bash /usr/local/bin/mirage-smoke.sh
