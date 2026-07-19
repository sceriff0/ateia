import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.grid import recover_grid
from illum.pipeline import Variant, run_variant, DEFAULT_MATRIX
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_run_periodic_variant_improves_seam():
    d = make_synthetic_mosaic(dark=200.0, vignette_strength=0.5, n_channels=2)
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    v = Variant(name="periodic-const", flatfield="periodic", dark="const")
    res = run_variant(d["mosaic"], g, v)
    assert res["corrected"].shape == d["mosaic"].shape
    m = res["metrics"]
    assert m["seam_x_after"] <= m["seam_x_before"]
    assert m["seconds"] >= 0


def test_default_matrix_nonempty_and_named():
    assert len(DEFAULT_MATRIX) >= 3
    assert len({v.name for v in DEFAULT_MATRIX}) == len(DEFAULT_MATRIX)
