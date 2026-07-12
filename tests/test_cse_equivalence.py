import importlib
import json
from pathlib import Path
import numpy as np
from tests.cse_fixture import make_fixture
from bin.utils.cse import single_method_eval


DATA = Path(__file__).parent / "data" / "cse"


def test_cse_imports():
    mod = importlib.import_module("bin.utils.cse")
    assert hasattr(mod, "single_method_eval")
    assert mod.__cse_upstream_version__ == "1.5.19"


def run_eval_on_fixture():
    img, mask, px = make_fixture()
    return single_method_eval(img, mask, PCA_model=False, output_dir=".",
                              pixelsizex=px, pixelsizey=px)


def flatten(metrics):
    flat = {}
    for k, v in metrics.items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                flat[f"{k}::{kk}"] = float(vv)
        else:
            flat[k] = float(v)
    return flat


def assert_metrics_close(result, golden, tol=1e-6, qs_rel_tol=1e-3):
    r, g = flatten(result), dict(golden)
    assert set(r) == set(g), f"metric keys differ: {set(r) ^ set(g)}"
    for key in g:
        gv, rv = g[key], r[key]
        if np.isnan(gv):
            assert np.isnan(rv), f"{key}: expected NaN"
        elif key == "QualityScore":
            # PCA+exp composite amplifies sub-epsilon float64-vs-float32 rounding
            # from the vectorized reductions; individual metrics stay strict at tol.
            denom = abs(gv) if abs(gv) > 1e-12 else 1.0
            assert abs(rv - gv) / denom <= qs_rel_tol, f"{key}: {rv} vs {gv} (rel {qs_rel_tol})"
        else:
            assert abs(rv - gv) <= tol, f"{key}: {rv} vs {gv}"


def test_fast_matches_golden():
    golden = json.loads((DATA / "golden_metrics.json").read_text())
    result = run_eval_on_fixture()
    assert_metrics_close(result, golden, tol=1e-6)
