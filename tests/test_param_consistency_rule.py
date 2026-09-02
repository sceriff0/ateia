# check_param_consistency.py and tests/nfmodel implement the same `params.x`
# rule. If they diverge, one of two guards starts lying and neither reports it.
"""Pin that check_param_consistency.py's `_refs_from_text` and
tests/nfmodel's `param_refs` agree on what counts as a `params.<name>` READ.

Both independently implement "a match immediately followed by `(` is a `Map`
method call (params.subMap(...)), not a param read" -- one for the CLI
checker's multi-directory glob, one for the shared source model other guards
build on. Nothing forces them to stay in sync; if a future edit to either
loosens or tightens the rule, this is the only test that would catch the
drift.

This file is deliberately a `test_*.py` module, not code inside
`check_param_consistency.py` itself -- pytest never collects a file whose
name doesn't match `test_*.py`, and a test placed there would silently never
run (the exact "guard that cannot fail" defect this remediation phase exists
to eliminate).
"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_param_consistency", ROOT / "tests" / "check_param_consistency.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_param_ref_rule_matches_the_shared_model():
    from tests.nfmodel import param_refs

    sample = "params.a + params.subMap(['b']) + params.c_d"
    assert param_refs(sample) == _mod._refs_from_text(sample)


def test_the_rule_actually_excludes_map_method_calls():
    # Guards the guard: if both implementations degraded to "any params.x",
    # the test above would still pass. Pin the behaviour itself.
    from tests.nfmodel import param_refs

    assert "subMap" not in param_refs("params.subMap(['b'])")
    assert "subMap" not in _mod._refs_from_text("params.subMap(['b'])")
