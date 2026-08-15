"""Small OME-TIFF I/O helpers shared by the tiled ('STARE') fan-out CLIs.

Keeps channel handling (promote 2-D to ``(C, H, W)``, pick the nuclear/fiducial channel, clamp to non-negative
and restore the source dtype on write) in one place so tiled_coarse / tiled_reg_tile / tiled_stitch
stay thin wrappers over the tested cores.
"""

from __future__ import annotations

import numpy as np

# There is deliberately no eager whole-slide reader here. `load_channels` used to
# sit at this spot -- `tifffile.imread` of the entire slide, promoted to (C, H, W) --
# and by the time TILED_COARSE and TILED_REG_TILE had both moved to `open_lazy`
# below, it had no caller anywhere under bin/. `tests/test_no_dead_bin_modules.py`
# exists to stop exactly that from accumulating, so it was deleted rather than kept
# "in case". The pre-change eager behaviour still has one legitimate use, as the
# oracle the lazy path is checked against, and it now lives in
# tests/test_tiled_reg_tile_lazy.py where that is all it can be.


def nuclear_channel(arr_chw, index):
    """Return the ``index`` channel of a ``(C, H, W)`` array as float32."""
    if not 0 <= index < arr_chw.shape[0]:
        raise ValueError(
            f"--nuclear-index {index} out of range for C={arr_chw.shape[0]}"
        )
    return arr_chw[index].astype(np.float32)


def open_lazy(path):
    """Open an OME-TIFF as a lazy ``(C, H, W)`` array + a close callable, without loading it.

    Uses tifffile's zarr view so region reads (``arr[c, y0:y1, x0:x1]``) fetch only the tiles they
    touch — the property the streaming gigapixel stitch relies on. A 2-D image is presented as
    ``C=1``; a pyramidal OME-TIFF resolves to its base (full-resolution) level.

    Returns ``(arr, dtype, close)`` where ``arr`` is a zarr array with a NumPy-like getitem.
    """
    import tifffile
    import zarr

    store = tifffile.imread(str(path), aszarr=True)
    grp = zarr.open(store, mode="r")
    arr = (
        grp[0] if isinstance(grp, zarr.hierarchy.Group) else grp
    )  # base level if pyramidal

    class _CHW:
        """Adapt a 2-D or (C,H,W) zarr array to a uniform (C,H,W) region-readable view."""

        def __init__(self, z):
            self._z = z
            self._2d = z.ndim == 2
            if z.ndim not in (2, 3):
                raise ValueError(
                    f"{path}: expected 2-D or 3-D (C,H,W), got shape {z.shape}"
                )
            self.shape = (1, *z.shape) if self._2d else tuple(z.shape)
            self.dtype = z.dtype

        def __getitem__(self, key):
            c, ys, xs = key
            return self._z[ys, xs] if self._2d else self._z[c, ys, xs]

    return _CHW(arr), arr.dtype, store.close


def decimation_factor(shapes, max_dim):
    """Return the single integer decimation factor that puts every shape under ``max_dim``.

    ``shapes`` is an iterable of ``(H, W)``. One *shared* factor is deliberate: the coarse anchor
    matches ORB descriptors between the reference and moving thumbnails, so both must sit at a
    common scale. Squeezing each slide independently under ``max_dim`` would introduce a scale
    change between the two thumbnails and bake a bogus scale term into M0.
    """
    if max_dim is None or max_dim <= 0:
        return 1
    longest = max(max(h, w) for h, w in shapes)
    return max(1, -(-longest // int(max_dim)))  # ceil division


# Byte budget for one streamed row band. Peak memory of a decimated read is this plus the
# (small) thumbnail, independent of slide width -- a fixed *row* count would not be, since a
# band of a 30k-wide slide is ~7x the same band of a 4k-wide one.
DEFAULT_BAND_BYTES = 64 * 1024 * 1024


def band_rows_for(width, factor, band_bytes=DEFAULT_BAND_BYTES):
    """Rows per streamed band: the most that fit in ``band_bytes`` as float32, snapped down to a
    multiple of ``factor`` (so the sampled rows stay globally aligned) and never below ``factor``.
    """
    factor = max(1, int(factor))
    per_row = max(1, int(width)) * 4  # float32
    rows = max(factor, int(band_bytes) // per_row)
    return max(factor, (rows // factor) * factor)


def read_decimated(src, index, factor, band_bytes=DEFAULT_BAND_BYTES):
    """Read channel ``index`` of a lazy ``(C, H, W)`` view as a ``factor``-decimated float32 plane.

    Streams the plane in row bands and strides each band, so peak memory is one band plus the
    thumbnail -- never the full-resolution plane. Band height is snapped to a multiple of
    ``factor`` so the rows sampled are globally ``0, factor, 2*factor, ...``; the bands therefore
    concatenate into exactly ``src[index, ::factor, ::factor]``.
    """
    import numpy as _np

    c_n, h, w = src.shape
    if not 0 <= index < c_n:
        raise ValueError(f"--nuclear-index {index} out of range for C={c_n}")
    factor = max(1, int(factor))
    band = band_rows_for(w, factor, band_bytes)
    rows = [
        _np.asarray(
            src[index, slice(y0, min(y0 + band, h)), slice(0, w)], dtype=_np.float32
        )[::factor, ::factor]
        for y0 in range(0, h, band)
    ]
    return rows[0] if len(rows) == 1 else _np.concatenate(rows, axis=0)
