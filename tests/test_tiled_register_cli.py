"""Smoke test for bin/tiled_register.py — the STARE registration CLI end to end.

Writes a synthetic reference/moving OME-TIFF pair, runs the CLI, and checks it produces a
registered slide (non-negative, source dtype, reference shape), a valid transform manifest, and a
TRE summary. Proves the CLI wiring around tiled_pipeline works with real file I/O.
"""

from __future__ import annotations

import json
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
pytest.importorskip("skimage")
pytest.importorskip("scipy")
tifffile = pytest.importorskip("tifffile")

import tiled_register  # noqa: E402


def _textured(seed, n=256):
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    img = gaussian_filter(rng.uniform(0, 1, size=(n, n)), 2.0)
    return (img - img.min()) / (img.ptp() + 1e-9)


def test_cli_registers_a_synthetic_pair(tmp_path):
    from scipy.ndimage import shift as ndi_shift

    dapi = _textured(0)
    marker = _textured(1)
    # reference: 2 channels (DAPI, a marker); moving: same content shifted by a known offset
    ref = (np.stack([dapi, marker]) * 60000).astype(np.uint16)
    shifted = np.stack(
        [
            ndi_shift(dapi, (5, -8), mode="reflect"),
            ndi_shift(marker, (5, -8), mode="reflect"),
        ]
    )
    mov = (np.clip(shifted, 0, 1) * 60000).astype(np.uint16)

    ref_f = tmp_path / "ref.ome.tiff"
    mov_f = tmp_path / "mov.ome.tiff"
    tifffile.imwrite(str(ref_f), ref, photometric="minisblack")
    tifffile.imwrite(str(mov_f), mov, photometric="minisblack")

    out_f = tmp_path / "mov_registered.ome.tiff"
    man_f = tmp_path / "manifest.json"
    tre_f = tmp_path / "tre.json"

    rc = tiled_register.main(
        [
            "--reference",
            str(ref_f),
            "--moving",
            str(mov_f),
            "--out",
            str(out_f),
            "--manifest",
            str(man_f),
            "--tre-summary",
            str(tre_f),
            "--dapi-index",
            "0",
            "--tile",
            "128",
            "--halo",
            "32",
            "--gate-tre",
            "0.0",
        ]
    )
    assert rc == 0

    registered = tifffile.imread(str(out_f))
    assert registered.shape == ref.shape  # (C, H, W) in the reference frame
    assert registered.dtype == np.uint16  # source dtype preserved
    assert registered.min() >= 0  # non-negativity guaranteed

    manifest = json.loads(man_f.read_text())
    assert "mov.ome.tiff" in manifest["slides"]
    assert np.asarray(manifest["slides"]["mov.ome.tiff"]["M0"]).shape == (3, 3)

    tre = json.loads(tre_f.read_text())
    assert tre["n_inliers"] >= 4
    assert tre["n_tiles"] >= 1
    # intrinsic TRE: coarse (rigid feature-fit) + per-tile rigid spatial heatmap
    assert "coarse_tre_px" in tre
    assert tre["rigid_tre_px"]["p50"] is not None
    assert len(tre["tiles"]) == tre["n_tiles"]  # one heatmap entry per tile
    assert {"ix", "iy", "cx", "cy", "tre_rigid"} <= set(tre["tiles"][0])
    # a shift is fully absorbed by M0 -> no mesh -> the rigid residual IS the final residual
    assert tre["residual_after_px"]["p50"] is not None
