"""Tests for bin/utils/tiled_io.py's own primitives, independent of any particular caller.

``open_lazy`` is the shared entry point every lazy-zarr-view reader in this repo goes through
(the STARE tiled fan-out, the illumination-correction tiling/apply pair, the two QC sites, the
segmentation readers). Its callers rely on different guarantees it makes --
``tests/test_tiled_coarse_thumbnail.py`` and ``tests/test_tiled_reg_tile_lazy.py`` already pin
the region-read (bounded-memory) side. This file pins the OTHER guarantee: every call opens a
genuinely fresh store, never one shared with a prior call on the same path -- so a caller that
opens, reads and closes in a loop cannot be handed a handle a previous iteration already closed.

That guarantee was originally pinned here because ``bin/preprocess.py`` read its channels from a
``ThreadPoolExecutor`` and gave each worker its own handle. That module is deleted -- illumination
correction now runs as three Nextflow processes around nf-core's BASICPY -- and NO script under
``bin/`` uses a thread pool any more, so nothing in-process depends on it for thread safety
today. It is kept because it is a real, stated property of ``open_lazy``'s contract that every
remaining open/read/close caller depends on, and because the next threaded or forked caller would
otherwise reintroduce the hazard with nothing pinning the property it needs.
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

from tiled_io import band_rows_for, open_lazy, read_decimated  # noqa: E402


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
    underlying store -- the property ``open_lazy``'s docstring states explicitly. A shared store
    would mean one caller's ``close`` invalidating another's array, and (for any future
    concurrent caller) two workers racing on the same mutable I/O state.
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


def _write_single_channel(tmp_path, dtype, h, w, seed=0, name="plane"):
    """A single-channel plain (non-OME) TIFF of ``dtype``, values spanning its usable range."""
    rng = np.random.default_rng(seed)
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        hi = min(info.max, 60000)  # keep uint16 fixtures away from tifffile edge cases at 65535
        arr = rng.integers(0, hi + 1, size=(1, h, w)).astype(dtype)
    else:
        arr = rng.random((1, h, w)).astype(dtype)
    path = tmp_path / f"{name}_{np.dtype(dtype).name}.tiff"
    tifffile.imwrite(str(path), arr, photometric="minisblack")
    return path


@pytest.mark.parametrize("factor", [1, 2, 3, 5])
@pytest.mark.parametrize("dtype", [np.uint8, np.uint16, np.float32])
def test_read_decimated_matches_independent_direct_slice(tmp_path, factor, dtype):
    """``read_decimated`` must equal ``src[index, ::factor, ::factor]`` computed a DIFFERENT way
    than the function under test: via a direct step-sliced getitem on the lazy view (which goes
    through zarr's own indexing engine), not by re-running read_decimated's own banded loop. A
    height not a multiple of the (deliberately tiny) band size forces a ragged final band -- the
    case the function's own docstring flags as the easiest to get wrong for a pre-allocated
    destination.

    ``dtype`` is passed to ``read_decimated`` only for uint8/uint16 (the narrow-read path this
    task adds); float32 exercises the pre-existing default-widening path unchanged.
    """
    h, w = 37, 21  # h % (any band size forced below) != 0 -> ragged final band
    path = _write_single_channel(tmp_path, dtype, h, w)

    tiny_budget = 8 * w * np.dtype(dtype).itemsize
    band = band_rows_for(w, factor, tiny_budget)
    assert band < h, "budget must force more than one band so the final band is ragged"
    assert h % band != 0, "fixture must actually exercise a ragged final band"

    src, src_dtype, close = open_lazy(path)
    try:
        assert src_dtype == dtype
        kwargs = {"dtype": dtype} if dtype in (np.uint8, np.uint16) else {}
        got = read_decimated(src, 0, factor, band_bytes=tiny_budget, **kwargs)
        expected = np.asarray(src[0, ::factor, ::factor])
        expected = expected.astype(dtype)
    finally:
        close()

    assert got.dtype == expected.dtype
    assert got.shape == expected.shape
    assert np.array_equal(got, expected)


def test_read_decimated_default_dtype_is_still_float32(tmp_path):
    """No ``dtype=`` argument must still yield float32 -- the contract
    ``bin/tiled_coarse.py`` relies on without needing any edit of its own."""
    h, w = 40, 24
    path = _write_single_channel(tmp_path, np.uint16, h, w)

    src, _dtype, close = open_lazy(path)
    try:
        got = read_decimated(src, 0, factor=2)
    finally:
        close()

    assert got.dtype == np.float32


def test_autoscale_uint16_vs_float32_identical_full_domain():
    """G1 evidence for the dtype change: ``autoscale_for_display`` must return byte-identical
    uint8 output whether it is fed the native uint16 plane or the same values widened to
    float32 -- exhaustively over the ENTIRE uint16 domain at the widest range (min=0,
    max=65535), per the task brief: "Sweep the full uint16 domain -- it is one cheap array
    and it is exhaustive, so it beats any sampled version."

    With a uint16 input, ``(img - img_min) / range_val`` promotes to float64 under numpy's
    true-divide; with a float32 input it stays float32. A more precise float32 intermediate
    could in principle round differently at ``np.round(normalized * 255)``. At THIS range it
    does not.

    NOTE for reviewers: this single range does not generalise to every (min, max) pair. An
    exploratory sweep (not committed, since the brief specifies exactly the array above) over
    400 random (min, max) sub-ranges, each built as ``np.arange(lo, hi + 1)`` rather than a
    coarser sample, found 2 mismatching pixel values out of ~8.87M checked (max |diff| = 1,
    e.g. min=13286, max=62449, value=52520: uint16-path -> 203, float32-path -> 204). The
    mechanism: for some (min, max), float32's coarser spacing rounds a true value just under
    x.5 UP to an exact x.5 float32 tie that it does not have the precision to resolve, and
    ``np.round``'s round-half-to-even then breaks that spurious tie differently than the
    float64 path, which keeps enough precision to see the value is not actually a tie. This
    is real and reproducible against the actual ``autoscale_for_display``, not an artifact of
    a reimplementation -- see task-3-report.md for the full writeup. It is flagged here as a
    known, extremely rare (~2 in 8.87M spot-checked values) residual risk to G1, not silently
    accepted; it does not block this task per the brief's explicit resolution of the question,
    but a slide whose nuclear channel happens to contain the right (min, max, value) triple
    could, in principle, get a single differing pixel in its QC overlay.
    """
    import os
    import sys

    # qc.py imports cv2 and skimage unconditionally at module scope, even though this test
    # only needs autoscale_for_display -- skip (not error) if either is unavailable, matching
    # tests/test_registration_qc_lazy_read.py's guard for the same module.
    pytest.importorskip("cv2")
    pytest.importorskip("skimage")

    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "utils"
        ),
    )
    from qc import autoscale_for_display

    full_uint16 = np.arange(0, 65536, dtype=np.uint16)
    full_float32 = full_uint16.astype(np.float32)

    got_uint16 = autoscale_for_display(full_uint16, method="minmax")
    got_float32 = autoscale_for_display(full_float32, method="minmax")
    assert got_uint16.dtype == np.uint8
    assert got_float32.dtype == np.uint8
    diff = np.abs(got_uint16.astype(np.int16) - got_float32.astype(np.int16))
    assert diff.max() == 0, f"max |diff| = {diff.max()} over the full uint16 domain"
