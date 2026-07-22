"""N4 bias-field variant: graceful-skip contract + (if SimpleITK present) a real run."""
import importlib, numpy as np, sys, pathlib
import pytest
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.pipeline import Variant, run_variant, full_matrix
from illum.grid import recover_grid
from generate_synthetic_mosaic import make_synthetic_mosaic

_HAS_SITK = importlib.util.find_spec("SimpleITK") is not None


def test_n4_module_imports_without_simpleitk():
    # Importing the module must never hard-fail (the SimpleITK import is guarded
    # inside n4_correct, not at module load) — mirrors basicpy/skimage handling.
    import illum.n4  # noqa: F401
    assert hasattr(illum.n4, "n4_correct")


@pytest.mark.skipif(_HAS_SITK, reason="SimpleITK present; the ImportError path can't trigger")
def test_n4_raises_importerror_when_simpleitk_absent():
    import illum.n4
    with pytest.raises(ImportError):
        illum.n4.n4_correct(np.ones((16, 16), np.uint16))


@pytest.mark.skipif(not _HAS_SITK, reason="SimpleITK not installed")
def test_n4_correct_returns_same_shape():
    d = make_synthetic_mosaic(tile=64, overlap=8, n_channels=1, vignette_strength=0.5,
                              structured=True)
    from illum.n4 import n4_correct
    out = n4_correct(d["mosaic"][0], out_dtype=np.uint16, shrink=2, iterations=(10, 10))
    assert out.shape == d["mosaic"][0].shape
    assert out.dtype == np.uint16


def test_full_matrix_includes_n4_and_basic_background_controls():
    names = [v.name for v in full_matrix(["none", "gaussian", "median"])]
    assert len(names) == len(set(names))                 # still unique
    for expected in ("baseline-basic-gaussian", "baseline-basic-median",
                     "baseline-n4", "baseline-n4-median"):
        assert expected in names
    # anchors sort ahead of the periodic candidates
    assert names.index("baseline-n4") < names.index("periodic_none_none")


def test_basic_background_control_is_basic_flatfield_plus_background():
    v = next(v for v in full_matrix(["none", "median"]) if v.name == "baseline-basic-median")
    assert (v.flatfield, v.dark, v.background) == ("basic", "none", "median")


@pytest.mark.skipif(not _HAS_SITK, reason="SimpleITK not installed")
def test_n4_variant_runs_end_to_end():
    d = make_synthetic_mosaic(tile=64, overlap=8, n_channels=1, vignette_strength=0.5,
                              structured=True)
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    res = run_variant(d["mosaic"], g, Variant("baseline-n4", flatfield="n4"))
    assert res["corrected"].shape == d["mosaic"].shape
    assert "fidelity" in res["metrics"]
