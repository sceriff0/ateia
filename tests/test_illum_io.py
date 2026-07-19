import numpy as np, sys, pathlib
import tifffile
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
from illum.io import load_mosaic


def test_tiff_cyx_with_explicit_channels(tmp_path):
    arr = np.zeros((2, 40, 50), dtype=np.uint16)
    src = tmp_path / "m.tif"
    tifffile.imwrite(src, arr, metadata={"axes": "CYX"})
    stack, names, px = load_mosaic(src, channels=["DAPI", "CD3"])
    assert stack.shape == (2, 40, 50)
    assert names == ["DAPI", "CD3"]
    assert px == 0.325                       # OME-less TIFF -> default pixel size


def test_tiff_yxc_reoriented_to_cyx_and_default_names(tmp_path):
    # a (Y, X, C) array with few channels must be reoriented to (C, Y, X)
    arr = np.zeros((40, 50, 3), dtype=np.uint16)
    src = tmp_path / "m.tif"
    tifffile.imwrite(src, arr)
    stack, names, _ = load_mosaic(src)        # no --channels -> ch{i}
    assert stack.shape == (3, 40, 50)
    assert names == ["ch0", "ch1", "ch2"]


def test_single_channel_2d_gets_channel_axis(tmp_path):
    arr = np.zeros((40, 50), dtype=np.uint16)
    src = tmp_path / "m.tif"
    tifffile.imwrite(src, arr)
    stack, names, _ = load_mosaic(src)
    assert stack.shape == (1, 40, 50)
    assert names == ["ch0"]


def test_explicit_channels_override_and_pad(tmp_path):
    arr = np.zeros((3, 20, 20), dtype=np.uint16)
    src = tmp_path / "m.tif"
    tifffile.imwrite(src, arr, metadata={"axes": "CYX"})
    # fewer names than channels -> padded with ch{i}
    stack, names, _ = load_mosaic(src, channels=["DAPI"])
    assert names == ["DAPI", "ch1", "ch2"]
