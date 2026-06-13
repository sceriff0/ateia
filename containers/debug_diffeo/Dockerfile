# Base image: CUDA 12.2.2 + cuDNN8 + Ubuntu 22.04
FROM nvidia/cuda:12.2.2-cudnn8-devel-ubuntu22.04

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# System dependencies
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
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Core scientific + GPU packages
RUN pip install --no-cache-dir \
    cupy-cuda12x \
    numpy==1.26.4 \
    scipy \
    pandas==2.2.3 \
    scikit-image==0.25.2 \
    aicsimageio==4.14.0 \
    gputools \
    edt

# TensorFlow GPU + Stardist ecosystem
RUN pip install --no-cache-dir \
    tensorflow==2.17.1 \ 
    stardist==0.9.1 \
    csbdeep \
    cellpose==3.1.1.1

# Optional GPU image segmentation tools
RUN pip install git+https://github.com/sceriff0/cudipy.git -v --no-deps

# Copy requirements if needed (optional)
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

RUN pip install imagecodecs
