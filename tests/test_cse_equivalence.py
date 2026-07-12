import importlib


def test_cse_imports():
    mod = importlib.import_module("bin.utils.cse")
    assert hasattr(mod, "single_method_eval")
    assert mod.__cse_upstream_version__ == "1.5.19"
