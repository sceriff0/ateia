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


def assert_metrics_close(result, golden, tol=1e-6):
    r, g = flatten(result), dict(golden)
    assert set(r) == set(g), f"metric keys differ: {set(r) ^ set(g)}"
    for key in g:
        if np.isnan(g[key]):
            assert np.isnan(r[key]), f"{key}: expected NaN"
        else:
            assert abs(r[key] - g[key]) <= tol, f"{key}: {r[key]} vs {g[key]}"


def test_fast_matches_golden():
    golden = json.loads((DATA / "golden_metrics.json").read_text())
    result = run_eval_on_fixture()
    assert_metrics_close(result, golden, tol=1e-6)
