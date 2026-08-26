# STARE learned global-alignment front-end: torch + kornia for DISK + LightGlue
# (bin/utils/coarse_align.py::_frontend_disk_lightglue), selected via `--reg_tiled_frontend
# disk_lightglue` under `-profile stare_ml` (nextflow.config).
#
# THIS IS A SECOND, OPTIONAL IMAGE. containers/tiled/Dockerfile is python:3.11-slim plus five
# CPU scientific-Python deps -- NO Java, NO BioFormats, NO libvips, NO GPU -- and that leanness
# is a design claim, not an accident: it is what makes STARE's tiled registration method
# deployable on a laptop or a low-end node where VALIS is not. torch + kornia alone are
# ~1-2.5 GB; folding them into the default :tiled image would retire that claim for every
# user, including the overwhelming majority who never select the learned front-end. So they
# ship ONLY here, and TILED_COARSE's container is switched to this image by -profile stare_ml
# (never by a boolean param -- Nextflow 26 delivers every --param as a String, so a
# `--use_learned_frontend false` flag would set the STRING "false", which is truthy).
#
# CPU-only wheel, deliberately: the shipped default is CPU (`torch --index-url
# https://download.pytorch.org/whl/cpu`), because the cluster this pipeline ships to never
# guarantees a GPU-visible node for TILED_COARSE, and the CPU path must stay correct since the
# default deployment never exercises anything else. `torch.cuda.is_available()` must report
# False out of this image; True would mean the wrong (CUDA) wheel got installed.
FROM python:3.11-slim

# procps: Nextflow's task-metrics wrapper (params.enable_trace, on by default) hard-exits
# BEFORE the script: block when `ps` is absent -- see containers/tiled/Dockerfile's comment
# and CHANGELOG.md for the exact failure mode (`:tiled` shipped without it once already).
# libgl1 / libglib2.0-0: torch/kornia's transitive OpenCV-adjacent wheels dlopen libGL at
# import time even in a headless container; without it `import kornia` fails at runtime, not
# at build time, which is a worse place to discover it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 procps \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir torch==2.3.1 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir kornia==0.7.3 \
    && python -c "import numpy, scipy, skimage, tifffile, zarr, torch, kornia; \
print('stare-ml image OK:', numpy.__version__, scipy.__version__, skimage.__version__, \
tifffile.__version__, zarr.__version__, torch.__version__, kornia.__version__, 'cuda_available=', torch.cuda.is_available())" \
    && ps -e -o pid= -o ppid= > /dev/null && echo "procps OK: nextflow task-metrics wrapper can run"

ENV PYTHONUNBUFFERED=1

# The bin/ scripts redirect numba/matplotlib caches to /tmp themselves (read-only-$HOME safe).
CMD ["python", "--version"]
