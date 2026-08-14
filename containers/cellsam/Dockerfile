FROM pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime

RUN apt-get update && apt-get install -y git

RUN python -m pip install --upgrade pip

# tifffile==2023.4.12 is left as-is (Task 11b, 2026-08-14): shared with
# containers/debug_diffeo (same value, same reasoning below). No image
# build is available here to verify a bump against `pip install git+.../
# cellSAM.git`'s own pinned requirements, which are not published on PyPI
# and so cannot be checked the way this task checked every other pin. See
# docs/containers.md.
RUN pip install \
    numpy==1.26.4 \
    pandas==2.2.2 \
    tifffile==2023.4.12

RUN python -m pip install -U "imagecodecs[all]"

RUN pip install git+https://github.com/vanvalenlab/cellSAM.git
