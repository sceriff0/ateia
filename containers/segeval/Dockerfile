# Segmentation-quality evaluation image — bolt3x/mirage-segeval:<manifest.version>
#
# Runs SEG_QUALITY_EVAL and MERGE_SEG_EVAL: reference-free 2D cell-segmentation
# quality scoring with the vendored CellSegmentationEvaluator subset under
# bin/utils/cse/ (upstream 1.5.19, Chen & Murphy, MBoC 2023). Scores a cell mask +
# nucleus mask against the multichannel image they were derived from and emits a
# composite QualityScore with NO ground-truth annotation.
#
# Pure CPU scientific-Python — no JVM, no BioFormats, no GPU, no libvips. The
# scorer's cost is RAM, not compute: it builds per-pixel index structures over the
# DECOMPRESSED masks, which is why conf/modules.config sizes the task off the image
# and why --cse_max_pixels exists to bin the input.
#
# OPT-IN in the pipeline (skip_seg_quality_eval defaults to true), so this image is
# only pulled by a run that asked for it.
FROM python:3.11-slim

LABEL description="Mirage SEG_QUALITY_EVAL: vendored CellSegmentationEvaluator (2D) metrics"

# procps is not optional. Nextflow's task-metrics wrapper hard-exits BEFORE the
# script block when `ps` is absent -- nextflow/executor/command-trace.txt does
#     command -v ps &>/dev/null || { >&2 echo "Command 'ps' required by nextflow ..."; exit 1; }
# so every task in this image would fail with exit status 1 and empty stdout as soon
# as the run collected metrics (-with-trace / -with-report -- i.e. every benchmark
# run, since benchmarks/configs/benchmark.config turns tracing on). python:*-slim
# ships no procps. Same reason it is installed in containers/tiled and spatialdata.
#
# NOTE: libgl1 / libglib2.0-0 used to be installed here. They are OpenCV's runtime
# deps, and nothing in this image imports cv2 -- the vendored CSE subset uses
# numpy/scipy/skimage/sklearn only. Removed rather than kept "just in case": an
# unexplained system package is indistinguishable from a load-bearing one later.
RUN apt-get update && apt-get install -y --no-install-recommends \
        procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt

# Fail the BUILD rather than a cluster run. This stack is exactly the kind that
# resolves in pip and then explodes on first import: scikit-image 0.25 against
# numpy 1.26 is a pairing pip will happily accept and then break on.
RUN python -c "\
import numpy, scipy, skimage, sklearn, pandas, tifffile, xmltodict; \
from sklearn.cluster import KMeans; \
from skimage.segmentation import find_boundaries; \
print('segeval image OK:', \
      'numpy', numpy.__version__, \
      '| scipy', scipy.__version__, \
      '| skimage', skimage.__version__, \
      '| sklearn', sklearn.__version__, \
      '| tifffile', tifffile.__version__)" \
    && ps -e -o pid= -o ppid= > /dev/null && echo "procps OK: nextflow task-metrics wrapper can run"

# Read-only $HOME safety. Several of these libraries default their caches to
# ~/.cache; on the cluster $HOME is read-only inside the container and a cache
# write turns into a crash far from its cause (the same class of failure as the
# VALIS JVM/jgo cache -- see docs/usage.md).
ENV XDG_CACHE_HOME=/tmp/.cache \
    MPLCONFIGDIR=/tmp/.mplconfig \
    NUMBA_CACHE_DIR=/tmp/.numba

CMD ["python", "--version"]
