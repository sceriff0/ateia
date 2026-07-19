import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.grid import recover_grid
from illum.flatfield import estimate_flatfield, apply_field
from illum.metrics import seam_peak, background_cv, measure
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_seam_peak_drops_after_correction():
    d = make_synthetic_mosaic(dark=0.0, vignette_strength=0.5, n_channels=1)
    ch = d["mosaic"][0]
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    ff = estimate_flatfield(ch, g)
    corr = apply_field(ch, ff, g)
    assert seam_peak(corr, g, axis=1) < seam_peak(ch, g, axis=1)


def test_measure_returns_time_and_memory():
    out, stats = measure(lambda a: a * 2, np.ones(1000))
    assert "seconds" in stats and stats["seconds"] >= 0
    assert "peak_rss_mb" in stats
