# SpatialData export image — bolt3x/attend_image_analysis:spatialdata
#
# Runs EXPORT_SPATIALDATA: serializes postprocessing output into a scverse-native
# SpatialData .zarr (labels + shapes + AnnData table, optionally the pyramid), and
# the JOIN_FLOWPATH attach step that folds QuPath/FlowPath gating results back in.
#
# CPU-only scientific-Python stack — no JVM, no GPU, no libvips. The heaviest thing
# here is the geopandas/shapely GEOS stack, which needs libgeos at runtime.
#
# NOTE the zarr major version: spatialdata >= 0.8.0 requires zarr >= 3, whereas the
# `tiled` image pins zarr 2.18 for tifffile's aszarr region reads. They are separate
# images precisely so those two constraints never have to be reconciled.
#
# Python 3.12, NOT the 3.11 the other CPU images (`tiled`) use:
# spatialdata 0.8.0 declares Requires-Python >= 3.12. On 3.11 pip silently skips it
# and reports "no matching distribution found for spatialdata>=0.8.0", which reads
# like a typo'd package name rather than a Python-version floor.
FROM python:3.12-slim

LABEL description="Mirage EXPORT_SPATIALDATA: SpatialData/AnnData/OME-NGFF serialization"

# libgeos-c1v5 -> shapely/geopandas; libexpat/procps are standard Nextflow runtime needs
# (procps supplies `ps`, which Nextflow calls to collect task metrics).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgeos-c1v5 \
        procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt

# Fail the BUILD rather than a cluster run: the import graph here (spatialdata ->
# zarr 3 -> ome_zarr -> dask, plus the GEOS shared library) is exactly the kind that
# resolves in pip and then explodes on first import.
RUN python -c "\
import spatialdata, anndata, geopandas, shapely, zarr, dask, tifffile, scipy, pandas, numpy; \
from spatialdata.models import Image2DModel, Labels2DModel, ShapesModel, TableModel; \
from spatialdata.transformations import Identity, Scale; \
print('spatialdata image OK:', \
      'spatialdata', spatialdata.__version__, \
      '| anndata', anndata.__version__, \
      '| geopandas', geopandas.__version__, \
      '| zarr', zarr.__version__, \
      '| dask', dask.__version__)"

# Read-only $HOME safety: several of these libraries default their caches to ~/.cache.
ENV XDG_CACHE_HOME=/tmp/.cache \
    MPLCONFIGDIR=/tmp/.mplconfig \
    NUMBA_CACHE_DIR=/tmp/.numba

CMD ["python", "--version"]
