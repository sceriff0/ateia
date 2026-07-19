"""Load a stitched mosaic as (C, Y, X) from ND2 or TIFF.

Returns the pixel array plus channel names and pixel size pulled from the
file's own metadata, so downstream code (pyramids, OME output) carries the
correct channel labels and micron scale. ND2 support uses the optional `nd2`
package, imported lazily so TIFF-only environments don't need it.
"""
from __future__ import annotations
import pathlib
import numpy as np
import tifffile

__all__ = ["load_mosaic"]

_DEFAULT_PIXEL_UM = 0.325


def _load_nd2(path):
    """Return (array, channel_names|None, pixel_um|None) from an ND2 file."""
    import nd2  # optional dependency — only needed for .nd2 inputs
    with nd2.ND2File(str(path)) as f:
        arr = f.asarray()
        names = None
        try:
            names = [c.channel.name for c in f.metadata.channels]
        except Exception:
            names = None
        pixel_um = None
        try:
            pixel_um = float(f.voxel_size().x)   # microns per pixel in X
        except Exception:
            pixel_um = None
    return arr, names, pixel_um


def _read_ome_pixel_size(path, fallback=_DEFAULT_PIXEL_UM):
    try:
        with tifffile.TiffFile(str(path)) as tif:
            if tif.ome_metadata:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(tif.ome_metadata)
                px = root.find(".//{*}Pixels")
                if px is not None and px.get("PhysicalSizeX"):
                    return float(px.get("PhysicalSizeX"))
    except Exception:
        pass
    return fallback


def _to_cyx(arr):
    """Squeeze singleton axes and orient as (C, Y, X)."""
    arr = np.squeeze(arr)
    if arr.ndim == 2:
        return arr[None]
    if arr.ndim != 3:
        raise ValueError(
            f"expected a 2D-per-channel mosaic; got shape {arr.shape} after squeeze "
            f"(Z-stacks / time series are not supported by the illum harness)"
        )
    # channel axis = the smallest non-spatial axis (few channels vs many pixels)
    cax = int(np.argmin(arr.shape))
    return np.moveaxis(arr, cax, 0)


def _resolve_names(explicit, meta_names, n_channels):
    src = explicit or meta_names
    if src:
        names = list(src)[:n_channels]
        if len(names) < n_channels:
            names += [f"ch{i}" for i in range(len(names), n_channels)]
        return names
    return [f"ch{i}" for i in range(n_channels)]


def load_mosaic(path, channels=None):
    """Load a mosaic as (stack (C,Y,X), channel_names, pixel_um).

    - `.nd2`  -> read via the `nd2` package; channel names and pixel size come
      from the ND2 metadata unless overridden by `channels`.
    - `.tif`/`.tiff` (and anything else) -> read via tifffile; pixel size from
      OME metadata (fallback 0.325 um); names from `channels` or `ch{i}`.

    Explicit `channels` always wins over metadata. Extra channels beyond the
    provided/known names are labelled `ch{i}`.
    """
    path = pathlib.Path(path)
    meta_names, pixel_um = None, None
    if path.suffix.lower() == ".nd2":
        arr, meta_names, pixel_um = _load_nd2(path)
    else:
        arr = tifffile.imread(str(path))
        pixel_um = _read_ome_pixel_size(path)
    stack = _to_cyx(arr)
    names = _resolve_names(channels, meta_names, stack.shape[0])
    if pixel_um is None or pixel_um <= 0:
        pixel_um = _DEFAULT_PIXEL_UM
    return stack, names, float(pixel_um)
