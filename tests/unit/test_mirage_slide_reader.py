"""MirageVipsSlideReader must round-trip mirage's own OME-TIFF writer exactly.

Runs inside mirage-valis:1.0.0 (needs pyvips + tifffile). Skips under a plain
python env (CI Python job) that lacks pyvips.
"""
import os
import sys
import tempfile

import numpy as np

try:
    import pytest
    pytest.importorskip("pyvips")
    pytest.importorskip("tifffile")
except ImportError:  # stdlib __main__ runner in-image (not under pytest)
    pass

import tifffile  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "utils"))

H, W, C = 300, 384, 4


def _make_slide(dirpath):
    """Write a fixture with bin/preprocess.py's exact settings. Returns (path, arr)."""
    rng = np.random.default_rng(7)
    arr = rng.integers(0, 65535, size=(C, H, W), dtype=np.uint16)
    p = os.path.join(dirpath, "s.ome.tiff")
    tifffile.imwrite(
        p, arr,
        photometric="minisblack",
        metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "SMA", "PANCK", "CD3"]}},
        bigtiff=True, ome=True, compression="zlib", tile=(256, 256),
    )
    return p, arr


def test_can_read_accepts_mirage_output():
    from mirage_slide_reader import MirageVipsSlideReader
    path, _ = _make_slide(tempfile.mkdtemp())
    assert MirageVipsSlideReader.can_read(path) is True


def test_slide2vips_matches_source_array():
    from mirage_slide_reader import MirageVipsSlideReader
    path, arr = _make_slide(tempfile.mkdtemp())
    img = MirageVipsSlideReader(path).slide2vips(level=0)
    assert (img.width, img.height, img.bands) == (W, H, C)
    got = np.frombuffer(img.write_to_memory(), dtype=np.uint16).reshape(H, W, C)
    assert np.array_equal(got, np.moveaxis(arr, 0, -1))


def test_slide2vips_region_matches_full_crop():
    from mirage_slide_reader import MirageVipsSlideReader
    path, arr = _make_slide(tempfile.mkdtemp())
    r = MirageVipsSlideReader(path)
    region = r.slide2vips(level=0, xywh=(10, 20, 64, 48))
    got = np.frombuffer(region.write_to_memory(), dtype=np.uint16).reshape(48, 64, C)
    assert np.array_equal(got, np.moveaxis(arr, 0, -1)[20:68, 10:74, :])


def test_metadata_reports_dims_and_channels():
    from mirage_slide_reader import MirageVipsSlideReader
    path, _ = _make_slide(tempfile.mkdtemp())
    md = MirageVipsSlideReader(path).metadata
    assert md.slide_dimensions[0] == (W, H)
    assert md.n_channels == C
    assert md.channel_names == ["DAPI", "SMA", "PANCK", "CD3"]
    assert md.is_rgb is False


def test_can_read_rejects_untiled():
    from mirage_slide_reader import MirageVipsSlideReader
    dirpath = tempfile.mkdtemp()
    p = os.path.join(dirpath, "flat.tiff")
    tifffile.imwrite(p, np.zeros((32, 32), dtype=np.uint16))
    assert MirageVipsSlideReader.can_read(p) is False


if __name__ == "__main__":
    # stdlib runner so this works in the VALIS image without pytest installed
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
