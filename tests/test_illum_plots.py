import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.grid import recover_grid
from illum.flatfield import estimate_flatfield, apply_field
from illum.plots import plot_grid_recovery, plot_flatfield, plot_before_after
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_plots_write_pngs(tmp_path):
    d = make_synthetic_mosaic(n_channels=1, vignette_strength=0.4, dark=0.0)
    ch = d["mosaic"][0]
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    ff = estimate_flatfield(ch, g)
    corr = apply_field(ch, ff, g)
    p1 = plot_grid_recovery(d["mosaic"], g, tmp_path / "grid.png", approx_tile=d["pitch"])
    p2 = plot_flatfield(ff, tmp_path / "ff.png", "CH0")
    p3 = plot_before_after(ch, corr, g, tmp_path / "ba.png", "CH0")
    for p in (p1, p2, p3):
        assert pathlib.Path(p).exists() and pathlib.Path(p).stat().st_size > 0
