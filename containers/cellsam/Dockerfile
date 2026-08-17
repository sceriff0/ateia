# torch 2.6.0 is the first release carrying the fix for CVE-2025-32434 (torch.load RCE);
# cellSAM downloads model weights, so that path is reachable. cuda12.1 is not published for
# 2.6.0 -- 12.4 is the nearest tag.
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

RUN apt-get update && apt-get install -y git

RUN python -m pip install --upgrade pip

# dask and scikit-image are named explicitly even though cellSAM's own dependency chain
# likely pulls scikit-image in transitively: segment_cellsam.py imports both directly
# (`import dask.array as da`, `from skimage import segmentation`), and an install nothing
# in this Dockerfile names is invisible to
# tests/test_container_harmonisation.py's "installs what it imports" guard.
RUN pip install \
    numpy==1.26.4 \
    pandas==2.3.3 \
    tifffile==2025.5.10 \
    zarr==2.18.3 \
    numcodecs==0.13.1 \
    dask==2026.7.1 \
    scikit-image==0.25.2

RUN python -m pip install "imagecodecs==2025.3.30"

# Pinned: an unpinned git install makes every rebuild a different image.
RUN pip install git+https://github.com/vanvalenlab/cellSAM.git@362a8e521bcd7313d5cb280bcd4cfc12977c1e9f
