import csv
from pathlib import Path

import numpy as np
import pytest
from benchmarks.generate_matrix import compute_target_shape, synthesize_channels


def test_compute_target_shape_scales_long_edge_preserving_aspect():
    # source 4000x2000 (HxW), target long edge 1000 -> 1000x500
    assert compute_target_shape((4000, 2000), 1000) == (1000, 500)


def test_compute_target_shape_long_edge_is_height_when_taller():
    assert compute_target_shape((2000, 4000), 1000) == (500, 1000)


def test_synthesize_channels_returns_requested_count_and_shape():
    src = np.full((8, 8), 100, dtype=np.uint8)
    out = synthesize_channels(src, n_channels=4, seed=0)
    assert out.shape == (4, 8, 8)
    assert out.dtype == np.uint8


def test_synthesize_channels_first_channel_is_source_unchanged():
    src = np.arange(16, dtype=np.uint8).reshape(4, 4)
    out = synthesize_channels(src, n_channels=3, seed=0)
    np.testing.assert_array_equal(out[0], src)


def test_synthesize_channels_extra_channels_differ_from_source():
    src = np.full((16, 16), 120, dtype=np.uint8)
    out = synthesize_channels(src, n_channels=2, seed=0)
    assert not np.array_equal(out[1], src)  # jitter+noise+offset perturbs it


def test_synthesize_channels_is_deterministic_for_seed():
    src = np.full((16, 16), 120, dtype=np.uint8)
    a = synthesize_channels(src, n_channels=3, seed=7)
    b = synthesize_channels(src, n_channels=3, seed=7)
    np.testing.assert_array_equal(a, b)


def test_synthesize_channels_single_channel_returns_unchanged_source():
    src = np.arange(64, dtype=np.uint8).reshape(8, 8)
    out = synthesize_channels(src, n_channels=1, seed=0)
    assert out.shape == (1, 8, 8)
    np.testing.assert_array_equal(out[0], src)


def test_synthesize_channels_output_stays_in_dtype_range():
    src = np.full((8, 8), 250, dtype=np.uint8)
    out = synthesize_channels(src, n_channels=4, seed=1)
    assert out.max() <= 255 and out.min() >= 0


def test_synthesize_channels_rejects_3d_input():
    with pytest.raises(ValueError):
        synthesize_channels(np.zeros((2, 4, 4), dtype=np.uint8), n_channels=2)


def test_synthesize_channels_rejects_zero_channels():
    with pytest.raises(ValueError):
        synthesize_channels(np.zeros((4, 4), dtype=np.uint8), n_channels=0)


def test_synthesize_channels_rejects_float_dtype():
    with pytest.raises(ValueError):
        synthesize_channels(np.zeros((4, 4), dtype=np.float32), n_channels=2)


def test_compute_target_shape_rejects_zero_dimension():
    with pytest.raises(ValueError):
        compute_target_shape((0, 0), 512)


def test_run_matrix_preserves_uint16_dtype(tmp_path):
    import tifffile
    from benchmarks.generate_matrix import run_matrix

    src = tmp_path / "src16.tif"
    tifffile.imwrite(src, np.full((200, 100), 4000, dtype=np.uint16))

    manifest = run_matrix(
        source=src, outdir=tmp_path / "m16",
        target_px=[50], n_channels=[1, 2], seed=0,
    )
    with open(manifest) as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        arr = tifffile.imread(r["path"])
        assert arr.dtype == np.uint16
        n, h, w = int(r["n_channels"]), int(r["height"]), int(r["width"])
        if n == 1:
            assert arr.shape == (h, w)
        else:
            assert arr.shape == (n, h, w)


def test_run_matrix_writes_cells_and_manifest(tmp_path):
    import tifffile
    from benchmarks.generate_matrix import run_matrix

    src = tmp_path / "src.tif"
    tifffile.imwrite(src, np.full((400, 200), 100, dtype=np.uint8))

    manifest = run_matrix(
        source=src, outdir=tmp_path / "matrix",
        target_px=[100, 50], n_channels=[1, 2], seed=0,
    )

    with open(manifest) as fh:
        rows = list(csv.DictReader(fh))
    # 2 sizes x 2 channel-counts = 4 cells
    assert len(rows) == 4
    assert set(rows[0].keys()) == {
        "cell_id", "target_px", "width", "height", "n_channels", "bytes", "path",
    }
    for r in rows:
        p = Path(r["path"])
        assert p.exists() and int(r["bytes"]) == p.stat().st_size
        arr = tifffile.imread(p)
        n = int(r["n_channels"])
        # single-channel cells are 2-D; multi-channel are (C, H, W)
        assert (arr.ndim == 2 and n == 1) or (arr.shape[0] == n)


def test_run_matrix_paired_writes_moving_with_distinct_channels(tmp_path):
    import tifffile
    from benchmarks.generate_matrix import run_matrix
    src = tmp_path / "s.tif"
    tifffile.imwrite(src, np.full((200, 100), 100, dtype=np.uint8))
    manifest = run_matrix(source=src, outdir=tmp_path / "m",
                          target_px=[50], n_channels=[1, 2], seed=0, paired=True)
    rows = {r["cell_id"]: r for r in csv.DictReader(open(manifest))}
    assert "moving_path" in next(iter(rows.values()))
    # n>=2 gets a moving file; n==1 does not (would collide on {DAPI})
    assert rows["px50_ch2"]["moving_path"] and Path(rows["px50_ch2"]["moving_path"]).exists()
    assert rows["px50_ch1"]["moving_path"] == ""
    mov = tifffile.imread(rows["px50_ch2"]["moving_path"])
    assert mov.shape[0] == 2  # (C,H,W)


def test_run_matrix_default_unpaired_manifest_columns_unchanged(tmp_path):
    import tifffile
    from benchmarks.generate_matrix import run_matrix
    src = tmp_path / "s.tif"
    tifffile.imwrite(src, np.full((80, 80), 100, dtype=np.uint8))
    manifest = run_matrix(source=src, outdir=tmp_path / "m", target_px=[40], n_channels=[2], seed=0)
    rows = list(csv.DictReader(open(manifest)))
    assert set(rows[0].keys()) == {"cell_id", "target_px", "width", "height", "n_channels", "bytes", "path"}
