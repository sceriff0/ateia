#!/usr/bin/env python3
"""Regression tests for bin/merge_channels_pyramid.py.

Guards the data-loss bug where a uint32 segmentation mask (as SEGMENT emits) was
silently truncated to uint16 when merged, wrapping label IDs > 65535 (mod 65536)
for slides with more than 65535 cells — exactly the large WSIs this pipeline
targets. The whole OME-TIFF must promote to uint32 when the mask needs it.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

# Heavy optional deps (cv2 / pyvips / tifffile codecs) may be absent in a bare
# Python CI env — skip gracefully there, like the valis/pyvips unit tests.
tifffile = pytest.importorskip("tifffile")
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "bin"))
mcp = pytest.importorskip("merge_channels_pyramid")


def _write_inputs(tmp_path, big_label):
    in_dir = tmp_path / "channels"
    in_dir.mkdir()
    h = w = 64
    # Two intensity channels (uint16), named so DAPI sorts first.
    tifffile.imwrite(in_dir / "00_DAPI.tif", np.full((h, w), 100, np.uint16))
    tifffile.imwrite(in_dir / "01_PANCK.tif", np.full((h, w), 200, np.uint16))
    # uint32 segmentation mask carrying a label ID above the uint16 ceiling.
    mask = np.zeros((h, w), np.uint32)
    mask[10:20, 10:20] = big_label
    mask[30:40, 30:40] = 5
    mask_path = tmp_path / "cell_mask.tif"
    tifffile.imwrite(mask_path, mask)
    return in_dir, mask_path


def test_uint32_segmentation_mask_not_truncated(tmp_path):
    big_label = 70000  # > 65535; uint16 wrap would give 70000 % 65536 = 4464
    in_dir, mask_path = _write_inputs(tmp_path, big_label)
    out_path = tmp_path / "pyramid.ome.tiff"

    mcp.merge_channels(
        input_dir=str(in_dir),
        output_path=str(out_path),
        segmentation_mask=str(mask_path),
        pyramid_resolutions=1,      # base level only — keep it fast
        compression="zlib",
    )

    arr = tifffile.imread(str(out_path))
    seg = arr[-1] if arr.ndim == 3 else arr  # Segmentation is the last channel
    assert seg.dtype == np.uint32, f"output should promote to uint32, got {seg.dtype}"
    assert int(seg.max()) == big_label, (
        f"label truncated: got {int(seg.max())}, expected {big_label} "
        f"(uint16 wrap would give {big_label % 65536})"
    )


def test_small_labels_stay_compact(tmp_path):
    # When all labels fit in uint16, the output need not promote to uint32.
    in_dir, mask_path = _write_inputs(tmp_path, big_label=500)
    out_path = tmp_path / "pyramid_small.ome.tiff"

    mcp.merge_channels(
        input_dir=str(in_dir),
        output_path=str(out_path),
        segmentation_mask=str(mask_path),
        pyramid_resolutions=1,
        compression="zlib",
    )

    arr = tifffile.imread(str(out_path))
    seg = arr[-1] if arr.ndim == 3 else arr
    assert int(seg.max()) == 500
