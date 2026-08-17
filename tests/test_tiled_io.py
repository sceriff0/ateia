"""Tests for bin/utils/tiled_io.py's own primitives, independent of any particular caller.

``open_lazy`` is the shared entry point every lazy-zarr-view reader in this repo goes through
(preprocess.py's per-channel worker threads, the STARE tiled fan-out, the two QC sites). Its
callers rely on different guarantees it makes -- ``tests/test_tiled_coarse_thumbnail.py`` and
``tests/test_tiled_reg_tile_lazy.py`` already pin the region-read (bounded-memory) side. This
file pins the OTHER guarantee: every call opens a genuinely fresh store, never one shared with
a prior call on the same path. ``bin/preprocess.py``'s thread-safety design (each worker thread
opens and closes its own handle -- see ``_read_and_process_channel_lazy`` and
``tests/test_preprocess_lazy_concurrency.py``) rests entirely on that guarantee, but until now
nothing pinned it at its actual source.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
)
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "utils"
    ),
)
pytest.importorskip("zarr")
tifffile = pytest.importorskip("tifffile")

from tiled_io import open_lazy  # noqa: E402


def _write_small_ome_tiff(tmp_path, n_channels=2, h=32, w=32):
    rng = np.random.default_rng(0)
    stack = rng.integers(0, 4000, size=(n_channels, h, w), dtype=np.uint16)
    path = tmp_path / "image.ome.tiff"
    tifffile.imwrite(
        str(path), stack, ome=True, photometric="minisblack", metadata={"axes": "CYX"}
    )
    return path


def test_open_lazy_returns_a_fresh_store_per_call(tmp_path):
    """Two ``open_lazy`` calls on the SAME path must return distinct objects, never the same
    underlying store -- the property ``open_lazy``'s docstring now states explicitly, and the
    one preprocess.py's per-thread-handle design (see its ``_read_and_process_channel_lazy``)
    depends on for thread safety: a shared store would mean two worker threads racing on the
    same mutable I/O state.
    """
    path = _write_small_ome_tiff(tmp_path)

    arr1, _dtype1, close1 = open_lazy(path)
    arr2, _dtype2, close2 = open_lazy(path)
    try:
        assert arr1 is not arr2, "open_lazy must build a fresh array/store on every call"
        assert close1 is not close2, "each call's close callable must belong to its own store"
        # Not just distinct wrapper objects -- distinct underlying zarr stores, so closing one
        # cannot invalidate reads through the other.
        assert arr1._z is not arr2._z
    finally:
        close1()
        close2()
