# STARE tiled registration image — bolt3x/attend_image_analysis:tiled
#
# Runs the fully-parallel, JVM-free registration method (registration_method='tiled'):
# TILED_REGISTER and WARP_SEG_QC's tiled backend (lib/WarpBackends.groovy). Pure CPU
# scientific-Python stack — NO Java/BioFormats, NO libvips, NO GPU — which is exactly what
# makes the method a laptop / low-end-node option and keeps this image an order of
# magnitude smaller than the VALIS image.
FROM python:3.11-slim

# scikit-image / scipy import cleanly on slim with no extra *Python-facing* system libs, but
# `procps` is not optional: Nextflow's task-metrics wrapper hard-exits BEFORE the script block
# when `ps` is absent -- nextflow/executor/command-trace.txt does
#     command -v ps &>/dev/null || { >&2 echo "Command 'ps' required by nextflow ..."; exit 1; }
# so every task in this image failed with exit status 1 and empty stdout as soon as the run
# collected metrics (-with-report / -with-trace, i.e. any run producing the resource report).
# python:*-slim ships no procps. Same reason it is installed in containers/spatialdata/Dockerfile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        procps \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && python -c "import numpy, scipy, skimage, tifffile, zarr; \
print('tiled image OK:', numpy.__version__, scipy.__version__, skimage.__version__, tifffile.__version__, zarr.__version__)" \
    && ps -e -o pid= -o ppid= > /dev/null && echo "procps OK: nextflow task-metrics wrapper can run"

# The bin/ scripts redirect numba/matplotlib caches to /tmp themselves (read-only-$HOME safe).
CMD ["python", "--version"]
