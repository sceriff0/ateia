import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.grid import recover_grid
from illum.pipeline import Variant, run_variant, DEFAULT_MATRIX, full_matrix
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


def test_full_matrix_has_visual_anchors_and_unique_names():
    variants = full_matrix(["none", "gaussian"])
    names = [v.name for v in variants]
    assert len(names) == len(set(names))            # no duplicate variants
    assert "baseline-uncorrected" in names          # the 'before' for visual QC
    assert "baseline-basic" in names                # the incumbent to beat
    # the uncorrected baseline must be a true raw passthrough (no correction)
    base = next(v for v in variants if v.name == "baseline-uncorrected")
    assert (base.flatfield, base.dark, base.background) == ("none", "none", "none")


def test_uncorrected_baseline_is_identity_passthrough():
    d = make_synthetic_mosaic(dark=200.0, vignette_strength=0.5, n_channels=1)
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    v = Variant(name="baseline-uncorrected", flatfield="none", dark="none", background="none")
    res = run_variant(d["mosaic"], g, v)
    # a "none" correction must return the input unchanged (the QuPath 'before')
    assert np.array_equal(res["corrected"], d["mosaic"])
    assert res["flats"][0] is None
