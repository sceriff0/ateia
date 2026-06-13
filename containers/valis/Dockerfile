# Dockerfile for VALIS-WSI
# Based on Ubuntu with all required dependencies

FROM ubuntu:22.04

# Set environment variables to prevent interactive prompts during installation
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Set Java environment variable
ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64

# Update and install basic system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    git \
    wget \
    curl \
    software-properties-common \
    pkg-config \
    autoconf \
    automake \
    libtool \
    && rm -rf /var/lib/apt/lists/*

# Install Python 3.10 (VALIS requires Python 3.9 or 3.10)
RUN add-apt-repository ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y \
    python3.10 \
    python3.10-dev \
    python3.10-distutils \
    python3.10-venv \
    && rm -rf /var/lib/apt/lists/*

# Install pip for Python 3.10
RUN curl -sS https://bootstrap.pypa.io/get-pip.py | python3.10

# Make Python 3.10 the default python3
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1 && \
    update-alternatives --install /usr/bin/python python /usr/bin/python3.10 1

# Install Java JDK (required for Bioformats)
RUN apt-get update && apt-get install -y \
    openjdk-11-jdk \
    && rm -rf /var/lib/apt/lists/*

# Install Maven (required for Bioformats)
RUN apt-get update && apt-get install -y \
    maven \
    && rm -rf /var/lib/apt/lists/*

# Install pixman 0.40.0 from source (required for OpenSlide to work correctly)
RUN wget https://cairographics.org/releases/pixman-0.40.0.tar.gz && \
    tar -xzf pixman-0.40.0.tar.gz && \
    cd pixman-0.40.0 && \
    ./configure && \
    make -j$(nproc) && \
    make install && \
    ldconfig && \
    cd .. && \
    rm -rf pixman-0.40.0*

# Install OpenSlide dependencies
RUN apt-get update && apt-get install -y \
    libcairo2-dev \
    libgdk-pixbuf2.0-dev \
    libxml2-dev \
    libsqlite3-dev \
    libjpeg-dev \
    libpng-dev \
    libtiff-dev \
    libopenjp2-7-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Install OpenSlide from source
RUN wget https://github.com/openslide/openslide/releases/download/v3.4.1/openslide-3.4.1.tar.gz && \
    tar -xzf openslide-3.4.1.tar.gz && \
    cd openslide-3.4.1 && \
    ./configure && \
    make -j$(nproc) && \
    make install && \
    ldconfig && \
    cd .. && \
    rm -rf openslide-3.4.1*

# Install dependencies for libvips
RUN apt-get update && apt-get install -y \
    libglib2.0-dev \
    libexpat1-dev \
    librsvg2-dev \
    libpoppler-glib-dev \
    libgsf-1-dev \
    liborc-0.4-dev \
    libwebp-dev \
    libheif-dev \
    libcfitsio-dev \
    libpango1.0-dev \
    libmatio-dev \
    libnifti-dev \
    libhdf5-dev \
    liblcms2-dev \
    libpng-dev \
    libjpeg-turbo8-dev \
    libopenexr-dev \
    libopenjp2-7-dev \
    libgirepository1.0-dev \
    gtk-doc-tools \
    gobject-introspection \
    && rm -rf /var/lib/apt/lists/*

# Install libffi-dev (needed for gobject-introspection)
RUN apt-get update && apt-get install -y \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install meson and ninja-build BEFORE building libvips (the build uses them).
# (Original ordering installed these after the first libvips attempt, which fails
#  under BuildKit with exit 127 before the fallback rebuild can run.)
RUN apt-get update && apt-get install -y \
    meson \
    ninja-build \
    && rm -rf /var/lib/apt/lists/*

# Install libvips from source (version >= 8.11 required)
# Using version 8.15.1 for stability. Non-fatal: the guarded rebuild below is the safety net.
RUN wget https://github.com/libvips/libvips/releases/download/v8.15.1/vips-8.15.1.tar.xz && \
    tar -xf vips-8.15.1.tar.xz && \
    cd vips-8.15.1 && \
    meson setup build --prefix=/usr/local && \
    cd build && \
    ninja && \
    ninja install && \
    ldconfig && \
    cd ../.. && \
    rm -rf vips-8.15.1* || echo "first libvips build failed; fallback will retry"

# Install meson and ninja-build (build tools for libvips)
RUN apt-get update && apt-get install -y \
    meson \
    ninja-build \
    && rm -rf /var/lib/apt/lists/*

# Re-build libvips with meson (if the previous step failed, this ensures it's built)
RUN if [ ! -f /usr/local/lib/libvips.so ]; then \
    wget https://github.com/libvips/libvips/releases/download/v8.15.1/vips-8.15.1.tar.xz && \
    tar -xf vips-8.15.1.tar.xz && \
    cd vips-8.15.1 && \
    meson setup build --prefix=/usr/local && \
    cd build && \
    ninja && \
    ninja install && \
    ldconfig && \
    cd ../.. && \
    rm -rf vips-8.15.1*; \
    fi

# Set PKG_CONFIG_PATH for libvips
ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:$PKG_CONFIG_PATH

# Update library cache
RUN ldconfig

# Install Python packages required by VALIS
RUN python3 -m pip install --upgrade pip setuptools wheel

# Install numpy first (often required by other packages)
RUN python3 -m pip install numpy

# Install VALIS-WSI and its dependencies
# Pinned to 1.0.0: the version in the deployed image and the bit-identical reference
# for distributed tiled registration. Unpinned would drift to 1.2.0 (different tile kernel).
RUN python3 -m pip install valis-wsi==1.0.0

# Create a working directory
WORKDIR /workspace

# Optional: Install Jupyter for interactive work
RUN python3 -m pip install jupyter jupyterlab

# Set the default command to bash
CMD ["/bin/bash"]

# ============================================================================
# OPTIONAL: SimpleElastix installation
# Uncomment the following section if you need SimpleElastix functionality
# Note: This significantly increases build time (1-2 hours)
# ============================================================================

# # Install SimpleElastix dependencies
# RUN apt-get update && apt-get install -y \
#     swig \
#     && rm -rf /var/lib/apt/lists/*
#
# # Clone and build SimpleElastix
# RUN git clone https://github.com/SuperElastix/SimpleElastix /tmp/SimpleElastix && \
#     mkdir /tmp/build && \
#     cd /tmp/build && \
#     cmake /tmp/SimpleElastix/SuperBuild \
#         -DWRAP_PYTHON=ON \
#         -DPYTHON_EXECUTABLE=/usr/bin/python3.10 \
#         -DPYTHON_INCLUDE_DIR=/usr/include/python3.10 \
#         -DPYTHON_LIBRARY=/usr/lib/x86_64-linux-gnu/libpython3.10.so && \
#     make -j2 && \
#     cd SimpleITK-build/Wrapping/Python && \
#     python3 Packaging/setup.py install && \
#     cd / && \
#     rm -rf /tmp/SimpleElastix /tmp/build

# ============================================================================
# Build instructions:
# docker build -t valis-wsi:latest .
#
# Run instructions:
# docker run -it --rm -v $(pwd):/workspace valis-wsi:latest
#
# With increased memory (recommended):
# docker run -it --rm --memory=20g -v $(pwd):/workspace valis-wsi:latest
#
# With Jupyter:
# docker run -it --rm -p 8888:8888 -v $(pwd):/workspace valis-wsi:latest jupyter lab --ip=0.0.0.0 --allow-root
# =============================================================================
