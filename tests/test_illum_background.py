import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.background import remove_background, available_methods
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_none_is_identity():
    d = make_synthetic_mosaic(n_channels=1)
    ch = d["mosaic"][0]
    out = remove_background(ch, "none")
    assert np.array_equal(out, ch)


def test_gaussian_background_removes_low_freq_gradient():
    d = make_synthetic_mosaic(bg_amp=800.0, vignette_strength=0.05, dark=0.0, n_channels=1)
    ch = d["mosaic"][0]
    out = remove_background(ch, "gaussian", sigma=40)
    # background subtraction should lower the dim-pixel mean
    assert float(np.percentile(out, 40)) < float(np.percentile(ch, 40))


def test_available_methods_includes_core():
    m = available_methods()
    assert {"none", "opening", "gaussian", "median"} <= set(m)
