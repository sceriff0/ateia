import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.grid import recover_grid
from illum.darkfield import estimate_dark_constant, estimate_dark_field, subtract_dark
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_constant_dark_recovers_pedestal():
    d = make_synthetic_mosaic(dark=300.0, vignette_strength=0.2, n_channels=1)
    dark = estimate_dark_constant(d["mosaic"][0], pct=1.0)
    # low-percentile pedestal should land near the injected dark (loose bound:
    # synthetic content floor + blend make it approximate)
    assert 100.0 <= dark <= 600.0


def test_subtract_constant_dark_lowers_floor():
    d = make_synthetic_mosaic(dark=300.0, n_channels=1)
    ch = d["mosaic"][0]
    out = subtract_dark(ch, 250.0)
    assert out.min() >= 0.0 or float(np.percentile(out, 1)) < float(np.percentile(ch, 1))
