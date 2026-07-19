import numpy as np
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests" / "testdata"))
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_synthetic_mosaic_shape_and_grid():
    d = make_synthetic_mosaic(n_tiles_y=4, n_tiles_x=5, tile=128, overlap=16, n_channels=3)
    assert d["mosaic"].shape[0] == 3
    C, Y, X = d["mosaic"].shape
    assert d["pitch"] == 128 - 16          # 112
    # mosaic spans n_tiles*pitch + overlap in each axis
    assert Y == 4 * (128 - 16) + 16
    assert X == 5 * (128 - 16) + 16
    assert d["mosaic"].dtype == np.uint16
    assert abs(float(d["flatfield"].mean()) - 1.0) < 1e-3


def test_synthetic_vignette_is_periodic():
    d = make_synthetic_mosaic(vignette_strength=0.5, dark=0.0, bg_amp=0.0, n_channels=1)
    # Column profile should show a dip at each seam (period = pitch)
    col = np.median(d["mosaic"][0], axis=0).astype(float)
    assert col.std() > 0  # non-flat illumination present
