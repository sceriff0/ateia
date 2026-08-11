"""Correctness + memory-shape tests for the lazy per-tile read in tiled_reg_tile.py.

tiled_reg_tile is the per-tile fan-out step: it runs once per tile (N tasks), unlike
tiled_stitch which runs once per slide. The old implementation called
``nuclear_channel(load_channels(path))`` on both the reference and moving slide on every
invocation — a full whole-slide decode, N times over. This mirrors what
test_tiled_stitch_streaming.py already pins for the stitch step: the lazy zarr-region-read
path must produce numerically identical output to the old full-decode path, while actually
bounding the region read.

Three properties are pinned here:
  1. Numerical equivalence: the lazy path's (dx, dy, tre) match an independently computed
     "old style" oracle (full nuclear_channel/load_channels + whole-image warp_image) exactly.
  2. Bounded read: for an interior tile that is 1/16 of the frame, the region actually read
     off the moving slide is a small fraction (< 25%) of the full slide — this is the test
     that fails against the pre-change code (it reads the whole slide, i.e. 100%).
  3. Edge tiles: a tile whose source_region maps entirely outside the moving slide produces
     an all-zero moving tile without raising.
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
pytest.importorskip("zarr")
tifffile = pytest.importorskip("tifffile")

import tiled_reg_tile  # noqa: E402
from tile_residual import residual_displacement  # noqa: E402
from tiled_io import load_channels, nuclear_channel  # noqa: E402
from tiled_warp import warp_image  # noqa: E402


def _textured(seed, n):
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    img = gaussian_filter(rng.uniform(0, 1, size=(n, n)), 2.0)
    return (img - img.min()) / (img.ptp() + 1e-9)


def _write_pair(tmp_path, n=512, translation=(3.0, -2.0), rotation_deg=1.0):
    """A synthetic 2-channel reference/moving OME-TIFF pair related by a known rigid M0."""
    from skimage.transform import EuclideanTransform
    from skimage.transform import warp as sk_warp

    dapi = _textured(0, n)
    marker = _textured(1, n)
    tform = EuclideanTransform(
        rotation=np.deg2rad(rotation_deg), translation=translation
    )
    ref = (np.stack([marker, dapi]) * 60000).astype(np.uint16)
    mov = (
        np.stack(
            [
                sk_warp(marker, tform, mode="reflect"),
                sk_warp(dapi, tform, mode="reflect"),
            ]
        )
        * 60000
    ).astype(np.uint16)

    ref_f = tmp_path / "ref.ome.tiff"
    mov_f = tmp_path / "mov.ome.tiff"
    tifffile.imwrite(str(ref_f), ref, photometric="minisblack")
    tifffile.imwrite(str(mov_f), mov, photometric="minisblack")

    # M0 is the *forward* moving->reference map; EuclideanTransform's own matrix is exactly
    # that convention (moving pixel x -> reference pixel M0 @ x).
    m0 = np.asarray(tform.params, dtype=float)
    m0_f = tmp_path / "m0.json"
    m0_f.write_text(json.dumps({"M0": m0.tolist()}))
    return ref_f, mov_f, m0_f, m0, ref, mov


def _old_style_oracle(ref_f, mov_f, m0, nuclear_index, rx0, ry0, rx1, ry1, upsample=10):
    """Reproduce the pre-change tiled_reg_tile.py logic verbatim: full decode, whole-image warp."""
    ref_nuc = nuclear_channel(load_channels(ref_f), nuclear_index)
    mov_nuc = nuclear_channel(load_channels(mov_f), nuclear_index)
    ref_tile = ref_nuc[ry0:ry1, rx0:rx1]
    mov_tile = warp_image(
        mov_nuc, m0, None, (ry1 - ry0, rx1 - rx0), out_origin=(rx0, ry0)
    )
    return residual_displacement(ref_tile, mov_tile, upsample=upsample)


def test_lazy_path_matches_old_full_decode_oracle(tmp_path):
    ref_f, mov_f, m0_f, m0, _ref, _mov = _write_pair(tmp_path)
    nuclear_index = 1
    rx0, ry0, rx1, ry1 = 192, 192, 320, 320  # interior tile, well clear of any edge

    dx_old, dy_old, tre_old = _old_style_oracle(
        ref_f, mov_f, m0, nuclear_index, rx0, ry0, rx1, ry1
    )

    out_f = tmp_path / "ctrl.json"
    tiled_reg_tile.main(
        [
            "--reference",
            str(ref_f),
            "--moving",
            str(mov_f),
            "--m0",
            str(m0_f),
            "--nuclear-index",
            str(nuclear_index),
            "--ix",
            "0",
            "--iy",
            "0",
            "--cx",
            "256.0",
            "--cy",
            "256.0",
            "--rx0",
            str(rx0),
            "--ry0",
            str(ry0),
            "--rx1",
            str(rx1),
            "--ry1",
            str(ry1),
            "--out",
            str(out_f),
        ]
    )
    result = json.loads(out_f.read_text())

    # keys and shape must be unchanged
    assert set(result.keys()) == {"ix", "iy", "cx", "cy", "dx", "dy", "tre"}
    assert result["ix"] == 0 and result["iy"] == 0
    assert result["cx"] == 256.0 and result["cy"] == 256.0

    assert result["dx"] == pytest.approx(dx_old, abs=1e-9)
    assert result["dy"] == pytest.approx(dy_old, abs=1e-9)
    assert result["tre"] == pytest.approx(tre_old, abs=1e-9)


def test_moving_slide_region_read_is_bounded(tmp_path, monkeypatch):
    """The moving-slide read must be a small fraction of the full slide for an interior tile.

    This is the test that fails against the pre-change code: nuclear_channel(load_channels(path))
    decodes the *entire* slide on every tile invocation, so the read region would be 100% of
    the full slide, not < 25%.
    """
    n = 512
    ref_f, mov_f, m0_f, _m0, _ref, mov = _write_pair(
        tmp_path, n=n, translation=(0.0, 0.0), rotation_deg=0.0
    )
    # tile is 1/16 of the (512, 512) frame, well inside the slide
    rx0, ry0, rx1, ry1 = 192, 192, 320, 320

    import tiled_io

    orig_open_lazy = tiled_io.open_lazy
    read_regions = []
    call_index = {"n": 0}

    def spying_open_lazy(path):
        arr, dtype, close = orig_open_lazy(path)
        call_index["n"] += 1
        is_moving = call_index["n"] == 2  # main() opens reference first, then moving
        if not is_moving:
            return arr, dtype, close

        class _Spy:
            def __init__(self, inner):
                self._inner = inner
                self.shape = inner.shape
                self.dtype = inner.dtype

            def __getitem__(self, key):
                _c, ys, xs = key
                read_regions.append((ys.stop - ys.start) * (xs.stop - xs.start))
                return self._inner[key]

        return _Spy(arr), dtype, close

    monkeypatch.setattr(tiled_reg_tile, "open_lazy", spying_open_lazy)

    out_f = tmp_path / "ctrl.json"
    tiled_reg_tile.main(
        [
            "--reference",
            str(ref_f),
            "--moving",
            str(mov_f),
            "--m0",
            str(m0_f),
            "--nuclear-index",
            "0",
            "--ix",
            "0",
            "--iy",
            "0",
            "--cx",
            "256.0",
            "--cy",
            "256.0",
            "--rx0",
            str(rx0),
            "--ry0",
            str(ry0),
            "--rx1",
            str(rx1),
            "--ry1",
            str(ry1),
            "--out",
            str(out_f),
        ]
    )

    assert read_regions, "expected at least one region read off the moving slide"
    full_area = mov.shape[-2] * mov.shape[-1]
    max_read = max(read_regions)
    assert max_read < 0.25 * full_area, (
        f"moving-slide read region {max_read}px is not bounded "
        f"(full slide is {full_area}px)"
    )


def test_edge_tile_outside_moving_slide_yields_zero_tile(tmp_path, monkeypatch):
    """A tile whose source_region falls entirely outside the moving slide must not raise."""
    n = 64
    ref_f, mov_f, m0_f, _m0, _ref, _mov = _write_pair(
        tmp_path, n=n, translation=(0.0, 0.0), rotation_deg=0.0
    )
    # Overwrite M0 with a huge translation so every reference-frame tile maps to moving
    # coordinates far outside the (64, 64) slide.
    huge_m0 = [[1.0, 0.0, 10_000.0], [0.0, 1.0, 10_000.0], [0.0, 0.0, 1.0]]
    m0_f.write_text(json.dumps({"M0": huge_m0}))

    captured = {}

    def fake_residual_displacement(ref_tile, mov_tile, upsample=10):
        captured["ref_tile"] = ref_tile
        captured["mov_tile"] = mov_tile
        return 0.0, 0.0, 0.0

    monkeypatch.setattr(
        tiled_reg_tile, "residual_displacement", fake_residual_displacement
    )

    out_f = tmp_path / "ctrl.json"
    # must not raise
    tiled_reg_tile.main(
        [
            "--reference",
            str(ref_f),
            "--moving",
            str(mov_f),
            "--m0",
            str(m0_f),
            "--nuclear-index",
            "0",
            "--ix",
            "0",
            "--iy",
            "0",
            "--cx",
            "16.0",
            "--cy",
            "16.0",
            "--rx0",
            "0",
            "--ry0",
            "0",
            "--rx1",
            "32",
            "--ry1",
            "32",
            "--out",
            str(out_f),
        ]
    )

    assert "mov_tile" in captured
    mov_tile = captured["mov_tile"]
    assert mov_tile.shape == (32, 32)
    assert np.array_equal(mov_tile, np.zeros((32, 32)))
    result = json.loads(out_f.read_text())
    assert set(result.keys()) == {"ix", "iy", "cx", "cy", "dx", "dy", "tre"}


def test_nuclear_index_out_of_range_raises_same_message(tmp_path):
    ref_f, mov_f, m0_f, _m0, _ref, _mov = _write_pair(tmp_path, n=64)
    out_f = tmp_path / "ctrl.json"
    with pytest.raises(ValueError, match=r"--nuclear-index 7 out of range for C=2"):
        tiled_reg_tile.main(
            [
                "--reference",
                str(ref_f),
                "--moving",
                str(mov_f),
                "--m0",
                str(m0_f),
                "--nuclear-index",
                "7",
                "--ix",
                "0",
                "--iy",
                "0",
                "--cx",
                "16.0",
                "--cy",
                "16.0",
                "--rx0",
                "0",
                "--ry0",
                "0",
                "--rx1",
                "32",
                "--ry1",
                "32",
                "--out",
                str(out_f),
            ]
        )


def _reg_tile_args(
    ref_f, mov_f, m0_f, out_f, nuclear_index=0, rx0=0, ry0=0, rx1=32, ry1=32
):
    """Small argv builder used only by the two close-callable tests below — the four tests above
    are left as-is (a full `_run(...)` refactor of the whole file is a deferred minor, out of
    scope for this fix)."""
    return [
        "--reference",
        str(ref_f),
        "--moving",
        str(mov_f),
        "--m0",
        str(m0_f),
        "--nuclear-index",
        str(nuclear_index),
        "--ix",
        "0",
        "--iy",
        "0",
        "--cx",
        "16.0",
        "--cy",
        "16.0",
        "--rx0",
        str(rx0),
        "--ry0",
        str(ry0),
        "--rx1",
        str(rx1),
        "--ry1",
        str(ry1),
        "--out",
        str(out_f),
    ]


def _spy_close(orig_open_lazy, closed, key_for_path):
    """Wrap tiled_io.open_lazy so the returned close callable is observed while still calling
    through to the real primitive — spying on the real close, not a mock that never opened
    anything."""

    def spying_open_lazy(path):
        arr, dtype, close = orig_open_lazy(path)
        key = key_for_path(path)

        def spy():
            closed[key] = True
            close()

        return arr, dtype, spy

    return spying_open_lazy


def test_reference_handle_closed_when_moving_open_raises(tmp_path, monkeypatch):
    """If open_lazy(a.moving) itself raises, the already-open reference handle must still close.

    Regression test for a review finding: the two open_lazy calls originally ran before a single
    try/finally, so a failure opening the moving slide (bad path, corrupt/unreadable TIFF,
    zarr-open failure) leaked the reference handle. Uses a real nonexistent path so the exception
    comes from the real primitive, not a simulated one.
    """
    ref_f, _mov_f, m0_f, _m0, _ref, _mov = _write_pair(tmp_path, n=64)
    bad_moving = tmp_path / "does_not_exist.ome.tiff"

    import tiled_io

    orig_open_lazy = tiled_io.open_lazy
    closed = {"ref": False}
    monkeypatch.setattr(
        tiled_reg_tile,
        "open_lazy",
        _spy_close(orig_open_lazy, closed, lambda path: "ref"),
    )

    out_f = tmp_path / "ctrl.json"
    with pytest.raises(FileNotFoundError):
        tiled_reg_tile.main(_reg_tile_args(ref_f, bad_moving, m0_f, out_f))

    assert closed["ref"] is True


def test_both_handles_closed_when_exception_raised_inside_try(tmp_path, monkeypatch):
    """Both opens succeed, then an exception inside the try block (out-of-range --nuclear-index)
    must still close both handles."""
    ref_f, mov_f, m0_f, _m0, _ref, _mov = _write_pair(tmp_path, n=64)

    import tiled_io

    orig_open_lazy = tiled_io.open_lazy
    closed = {"ref": False, "mov": False}

    def key_for_path(path):
        return "ref" if str(path) == str(ref_f) else "mov"

    monkeypatch.setattr(
        tiled_reg_tile,
        "open_lazy",
        _spy_close(orig_open_lazy, closed, key_for_path),
    )

    out_f = tmp_path / "ctrl.json"
    with pytest.raises(ValueError, match="out of range"):
        tiled_reg_tile.main(_reg_tile_args(ref_f, mov_f, m0_f, out_f, nuclear_index=99))

    assert closed["ref"] is True
    assert closed["mov"] is True
