# STARE tiled registration image — bolt3x/mirage-tiled:1.0.0
#
# Runs the fully-parallel, JVM-free registration method (registration_method='tiled'):
# TILED_COARSE, TILED_REG_TILE, TILED_SOLVE, TILED_STITCH and WARP_SEG_QC's tiled backend
# (lib/WarpBackends.groovy). (The single-task TILED_REGISTER this comment used to name was
# removed with the reg_tiled_fanout flag — see CHANGELOG.)
# NO Java/BioFormats, NO libvips, NO GPU — that is what keeps this image an order of
# magnitude smaller than the VALIS image. It no longer makes the METHOD laptop-sized:
# TILED_COARSE's DISK matcher asks ~48 GB at the shipped reg_tiled_mode=high tier
# (~5 GB at low), so STARE is a cluster backend at its default.
#
# THIS IMAGE NOW CARRIES torch + kornia (CPU wheels), and the earlier "lean, pure-CPU
# scientific-Python, torch-free" claim is deliberately RETIRED. They power the DISK +
# LightGlue COARSE front-end (bin/utils/coarse_align.py::_frontend_disk_lightglue), which
# until now lived in a second, optional image (containers/stare-ml, `-profile stare_ml`).
# That split is gone: ONE image that always works beats TWO where the second is never
# published. The second image was built by a plain push to main but NEVER pushed
# (.github/workflows/build-images.yml pushes only on release:published /
# workflow_dispatch), so `-profile stare_ml` failed on image pull for anyone who had not
# manually dispatched the build — the exact trap `:tiled` itself shipped into once
# already. Folding torch+kornia in here costs every user ~1 GB of pull; it buys every user
# a front-end that runs. The image still has no JVM, no libvips and no CUDA runtime.
#
# CPU-ONLY, deliberately: torch comes from `--index-url https://download.pytorch.org/whl/cpu`
# because the cluster this pipeline ships to never guarantees a GPU-visible node for
# TILED_COARSE, and the CPU path must stay correct since the default deployment exercises
# nothing else. `torch.cuda.is_available()` must report False out of this image; True means
# the wrong (CUDA) wheel got installed, and the final verification RUN below asserts it.
FROM python:3.11-slim

# scikit-image / scipy import cleanly on slim with no extra *Python-facing* system libs, but
# `procps` is not optional: Nextflow's task-metrics wrapper hard-exits BEFORE the script block
# when `ps` is absent -- nextflow/executor/command-trace.txt does
#     command -v ps &>/dev/null || { >&2 echo "Command 'ps' required by nextflow ..."; exit 1; }
# so every task in this image failed with exit status 1 and empty stdout as soon as the run
# collected metrics (-with-report / -with-trace, i.e. any run producing the resource report).
# python:*-slim ships no procps. Same reason it is installed in containers/spatialdata/Dockerfile.
#
# libgl1 / libglib2.0-0: torch/kornia's transitive OpenCV-adjacent wheels dlopen libGL at
# import time even in a headless container; without them `import kornia` fails at RUNTIME,
# not at build time, which is a worse place to discover it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 procps \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt /tmp/requirements.txt
# torch BEFORE kornia, and from the CPU index: kornia pulls torch from PyPI (the CUDA
# wheel, ~2.5 GB) if torch is not already satisfied. The order is load-bearing, and it is
# why these two pins live HERE and not in requirements.txt -- see that file's comment.
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir torch==2.3.1 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir kornia==0.7.3

# BAKE THE PRETRAINED WEIGHTS. kornia fetches these at FIRST USE into
# $TORCH_HOME/hub/checkpoints -- from raw.githubusercontent.com (DISK) and
# github.com/cvg/LightGlue/releases (LightGlue). The cluster has a READ-ONLY $HOME and
# docs/usage.md documents air-gapped execution, so a first run would die there with no
# network and nowhere to write. TORCH_HOME is set to /opt/torch, outside $HOME, and made
# world-readable so any task UID can read it (docker.runOptions passes -u $(id -u)).
#
# The two filenames are the ones torch.hub derives from the download URLs, so the LightGlue
# checkpoint is `disk_lightglue_v0-1_arxiv-pth` -- no dot before `pth`. That is not a typo;
# do not "correct" it.
#
# This is a CONTAINER-ONLY fix. A git pull can never repair a missing checkpoint.
# Guarded by tests/test_disk_weights_are_baked.py.
ENV TORCH_HOME=/opt/torch
RUN python -c 'from kornia.feature import DISK, LightGlue; DISK.from_pretrained("depth"); LightGlue("disk")' \
    && test -f /opt/torch/hub/checkpoints/depth-save.pth \
    && test -f /opt/torch/hub/checkpoints/disk_lightglue_v0-1_arxiv-pth \
    && chmod -R a+rX /opt/torch \
    && HOME=/nonexistent python -c 'from kornia.feature import DISK, LightGlue; DISK.from_pretrained("depth"); LightGlue("disk"); print("baked weights load with no HOME")'

RUN python -c "import numpy, scipy, skimage, tifffile, zarr, torch, kornia; \
assert not torch.cuda.is_available(), 'CUDA wheel installed -- expected the CPU wheel'; \
print('tiled image OK:', numpy.__version__, scipy.__version__, skimage.__version__, tifffile.__version__, zarr.__version__, torch.__version__, kornia.__version__)" \
    && ps -e -o pid= -o ppid= > /dev/null && echo "procps OK: nextflow task-metrics wrapper can run"

# Carried over from the folded-in stare-ml image, which set it: TILED_COARSE used to run
# there under -profile stare_ml and now runs here, so keeping it preserves that path's
# behaviour. Unbuffered stdout also means a long COARSE task's progress lines reach
# Nextflow's .command.out as they happen rather than at exit.
ENV PYTHONUNBUFFERED=1

# The bin/ scripts redirect numba/matplotlib caches to /tmp themselves (read-only-$HOME safe).
CMD ["python", "--version"]
