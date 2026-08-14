
# Option 1: NVIDIA CUDA base (most reliable)
FROM nvidia/cuda:12.2.2-devel-ubuntu22.04

# Install Python and essential tools
RUN apt-get update && apt-get install -y \
    python3.10 \
    python3-pip \
    python3.10-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create python symlink
RUN ln -s /usr/bin/python3.10 /usr/bin/python

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# Install packages.
#
# cupy-cuda12x, cucim and aicsimageio (both the bare install and the [all]
# extra) were removed 2026-08-14 (Task 11b): a full grep of every bin/*.py
# and bin/utils/*.py script that this container's processes actually run
# (QUANTIFY, EXTRACT_CELL_PROPERTIES, EXTRACT_NUCLEI_PROPERTIES,
# EXPORT_GEOJSON, MERGE_QUANT_CSVS, GENERATE_POSTPROCESSING_QC,
# SEG_QC_GEOJSON -> quantify.py, extract_cell_properties.py, export_geojson.py,
# merge_quant_csvs.py, generate_postprocessing_qc.py, mask_to_geojson.py, and
# their shared bin/utils/*.py helpers) found zero imports of cupy, cucim or
# aicsimageio, direct or transitive. `grep -rn "cupy\|cucim\|aicsimageio" bin/`
# returns nothing. If a future script starts using one of these, re-add it
# here rather than reintroducing it "just in case".
#
# tifffile is pinned explicitly (was unpinned before Task 11b) at 2024.7.2,
# converging with containers/tiled, containers/merge and containers/istantseg
# -- see docs/containers.md.
RUN pip install --no-cache-dir \
    numpy \
    pandas \
    scikit-image \
    scipy \
    matplotlib \
    tqdm \
    psutil \
    tifffile==2024.7.2
