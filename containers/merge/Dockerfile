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
#
# tifffile is pinned explicitly (it used to float to whatever was newest at
# build time -- MERGE_AND_PYRAMID is the pipeline's one true pyramid writer,
# so an unpinned tifffile here means the published OME-TIFF's exact bytes
# can change on a rebuild with no code change). 2024.7.2 converges this
# image with containers/tiled, containers/quantification and
# containers/istantseg -- see docs/containers.md for the full pin table and
# why the other images (bioformats, preprocess, cellsam, debug_diffeo,
# segmentation) could not safely move to it without a build to verify
# against their own already-pinned scientific stacks.
RUN pip install --no-cache-dir \
        numpy \
        numba \
        tifffile==2024.7.2 \
        imagecodecs \
	zarr
