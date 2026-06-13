"""Lossless float32 displacement-field I/O via pyvips `.v` (native, uncompressed).

Per spec §5 precondition 2: the on-disk container for per-tile displacement fields exchanged
between REG_TILE and REG_STITCH must round-trip float32 EXACTLY — no quantization, no lossy
compression — or the stitched field diverges from classic VALIS. pyvips `.v` is the raw native
format (no codec), and warp_tools.numpy2vips/vips2numpy are the exact helpers VALIS itself uses
to move tile fields between numpy and pyvips (non_rigid_registrars.py:1347).
"""
import numpy as np
from valis import warp_tools


def write_field(field_hw2, path):
    """Write an (H, W, 2) float32 displacement field losslessly to a pyvips `.v` file."""
    arr = np.ascontiguousarray(np.asarray(field_hw2).astype(np.float32))
    warp_tools.numpy2vips(arr).write_to_file(path)


def read_field(path):
    """Read a `.v` displacement field back as an (H, W, 2) float32 ndarray."""
    import pyvips
    vi = pyvips.Image.new_from_file(path)
    return np.ascontiguousarray(warp_tools.vips2numpy(vi).astype(np.float32))
