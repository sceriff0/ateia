FROM cdgatenbee/valis-wsi:1.0.0

# Install required native dependencies for imagecodecs
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libjpeg-dev \
        libopenjp2-7-dev \
        libzstd-dev \
        libwebp-dev \
        libtiff-dev \
        libpng-dev \
        liblzma-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies directly
RUN pip install --no-cache-dir \
        numpy \
        numba \
        tifffile \
        imagecodecs \
	zarr
