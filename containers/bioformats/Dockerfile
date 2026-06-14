# Bioformats-native CONVERT_IMAGE container for the mirage pipeline.
#
# Build: docker build -t bolt3x/attend_image_analysis:bioformats_v1 -f containers/convert_image/Dockerfile .
# Push:  docker push bolt3x/attend_image_analysis:bioformats_v1
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

RUN pip3 install --no-cache-dir \
        tifffile==2024.7.24 \
	imagecodecs \
        numpy==1.26.4


