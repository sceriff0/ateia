"""Small OME-TIFF I/O helpers shared by the tiled ('STARE') fan-out CLIs.

Keeps channel handling (promote 2-D to ``(C, H, W)``, pick the DAPI channel, clamp to non-negative
and restore the source dtype on write) in one place so tiled_coarse / tiled_reg_tile / tiled_stitch
stay thin wrappers over the tested cores.
"""

from __future__ import annotations

import numpy as np


def load_channels(path):
    """Read an OME-TIFF as ``(C, H, W)`` (a 2-D image is promoted to ``C=1``)."""
    import tifffile

    arr = np.asarray(tifffile.imread(str(path)))
    if arr.ndim == 2:
        arr = arr[np.newaxis, ...]
    elif arr.ndim != 3:
        raise ValueError(f"{path}: expected 2-D or 3-D (C,H,W), got shape {arr.shape}")
    return arr


def dapi_channel(arr_chw, index):
    """Return the ``index`` channel of a ``(C, H, W)`` array as float32."""
    if not 0 <= index < arr_chw.shape[0]:
        raise ValueError(f"--dapi-index {index} out of range for C={arr_chw.shape[0]}")
    return arr_chw[index].astype(np.float32)


def write_ome_nonneg(path, arr_chw, src_dtype):
    """Write a ``(C, H, W)`` array as OME-TIFF, clamped non-negative and cast to ``src_dtype``.

    Bilinear warping keeps values within the source range, but clamp + round here as the final
    guarantee that downstream quantification never sees a negative pixel.
    """
    import tifffile

    out = np.clip(np.asarray(arr_chw, dtype=float), 0.0, None)
    if np.issubdtype(src_dtype, np.integer):
        info = np.iinfo(src_dtype)
        out = np.clip(np.rint(out), info.min, info.max)
    tifffile.imwrite(str(path), out.astype(src_dtype), photometric="minisblack")
