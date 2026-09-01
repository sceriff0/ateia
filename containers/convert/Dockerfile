# Bioformats-native CONVERT_IMAGE container for the mirage pipeline.
#
# Build: docker build -t bolt3x/mirage-convert:1.0.0 -f containers/convert/Dockerfile .
# Push:  docker push bolt3x/mirage-convert:1.0.0
#
# Image contents:
#   - eclipse-temurin:21-jre-jammy  (LTS JRE)
#   - bioformats2raw (Glencoe, any Bioformats-readable format -> OME-Zarr)
#   - raw2ometiff   (Glencoe, OME-Zarr -> OME-TIFF)
#   - python3 + tifffile + numpy (channel reorder step)
#
# No python-bioformats / javabridge / scyjava — both Glencoe tools are
# shaded JVM zips; install is `unzip` + symlink, no native compilation.

FROM eclipse-temurin:21-jre-jammy

LABEL maintainer="mirage WSI pipeline"
LABEL description="Bioformats CONVERT_IMAGE: bioformats2raw + raw2ometiff + tifffile reorder"

ARG BF2RAW_VERSION=0.12.0
ARG R2OT_VERSION=0.10.0

RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
        unzip \
        python3 \
        python3-pip \
        ca-certificates \
        coreutils \
    && rm -rf /var/lib/apt/lists/*

RUN wget -q "https://github.com/glencoesoftware/bioformats2raw/releases/download/v${BF2RAW_VERSION}/bioformats2raw-${BF2RAW_VERSION}.zip" \
    && unzip -q "bioformats2raw-${BF2RAW_VERSION}.zip" -d /opt \
    && ln -s "/opt/bioformats2raw-${BF2RAW_VERSION}/bin/bioformats2raw" /usr/local/bin/bioformats2raw \
    && rm "bioformats2raw-${BF2RAW_VERSION}.zip"

RUN wget -q "https://github.com/glencoesoftware/raw2ometiff/releases/download/v${R2OT_VERSION}/raw2ometiff-${R2OT_VERSION}.zip" \
    && unzip -q "raw2ometiff-${R2OT_VERSION}.zip" -d /opt \
    && ln -s "/opt/raw2ometiff-${R2OT_VERSION}/bin/raw2ometiff" /usr/local/bin/raw2ometiff \
    && rm "raw2ometiff-${R2OT_VERSION}.zip"

# Upgrade pip first: jammy ships 22.0.2, whose resolver reports this package set's conflict as
# an opaque "nd2[legacy]==0.11.2 and nd2[legacy]==0.11.3" rather than naming the real cap.
RUN pip3 install --no-cache-dir --upgrade pip

# dask is pinned EXPLICITLY even though every bioio package already pulls it in: all six
# ask only for `dask[array]>=2021.4.1`, with no upper bound, so an unpinned build silently
# takes whatever the newest dask supporting this base's Python 3.10 happens to be on build
# day. bin/convert_image.py's read is dask (BioImage.dask_data) and its write iterates that
# array's CHUNKS, so dask's version is load-bearing for this image, not incidental.
# 2026.7.1 is the version containers/stardist and containers/cellsam already pin, and the
# one CI pins. Resolution verified against this image's own pins before committing --
# `pip install --dry-run --python-version 3.10 --platform manylinux_2_17_x86_64` over
# numpy==2.2.6 + bioio==3.5.0 + dask[array]==2026.7.1 resolves, picking dask 2026.7.1
# (dask[array] 2026.7.1 needs numpy>=1.24 and Python>=3.10; both hold here).
#
# numpy and tifffile diverge from the harmonised set here, and both are forced:
#   * bioio-ome-tiff 1.4.0 requires tifffile[zarr]<2025.1.10 on python_version < "3.11", and this
#     base (eclipse-temurin:21-jre-jammy) is Python 3.10. 2024.12.12 is the newest that satisfies it.
#   * the bioio 3.5.0 plugin set cannot resolve against numpy 1.26.4 -- bioio-lif's dask chain is
#     unsatisfiable there. numpy 2 is safe in THIS image specifically: it carries no TensorFlow or
#     StarDist, which are the only reason the harmonised numpy is held at the last 1.x.
# Verified by resolving the exact set with pip 26 on Python 3.10 before committing.
#
# THIS IMAGE TAKES NO requirements/constraints.txt, and that is deliberate -- it is the
# second documented exception alongside containers/spatialdata. Both divergences above are
# already recorded in tests/test_container_harmonisation.py's PINNED_EXCEPTIONS, each with
# the upstream constraint that forces it, and tests/test_ci_stack_pinned.py derives its
# exception list from that table rather than from a hand-written name list. Passing
# `-c /tmp/requirements/constraints.txt` here would not "let the constraint win" -- pip
# would report ResolutionImpossible on numpy and tifffile and the image would not build.
RUN pip3 install --no-cache-dir \
        tifffile==2024.12.12 \
        imagecodecs==2025.3.30 \
        numpy==2.2.6 \
        zarr==2.18.3 \
        numcodecs==0.13.1 \
        h5py==3.11.0 \
        bioio==3.5.0 \
        bioio-ome-tiff==1.4.0 \
        bioio-tifffile==1.3.0 \
        bioio-nd2==2.4.0 \
        bioio-czi==2.9.0 \
        bioio-lif==1.5.0 \
        dask[array]==2026.7.1


