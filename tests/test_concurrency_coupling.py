# max_forks and queue_size are only meaningful together -- the lower binds.
#
# They shipped as two independent params with a 5/20 default and two in-tree
# comments both claiming 100, so the effective cluster throughput was ~4x lower
# than the documentation said and nobody could see it from either knob alone.
#
# Assertions run against tests.nfmodel's comment/string-stripped views, not raw
# text -- tests/test_nfmodel.py's anti-vacuity guard requires every new guard
# that reads Nextflow/Groovy source to query the shared model rather than
# re-implementing its own private parse, and a naive regex over raw text can be
# fooled by a matching comment the same way the four guards documented there
# were.
import re

from tests.nfmodel import REPO_ROOT, strip_comments, with_name_blocks

CFG = strip_comments((REPO_ROOT / "nextflow.config").read_text())


def test_the_individual_caps_are_null_declared():
    # A numeric default here would be computed BEFORE the CLI is applied, so
    # --concurrency would be silently ignored. Null-declared, then derived in
    # the executor/process scopes, is the only ordering that works.
    for name in ("max_forks", "queue_size"):
        assert re.search(rf"^\s*{name}\s*=\s*null", CFG, re.M), (
            f"{name} must be declared null and derived from concurrency"
        )


def test_concurrency_is_declared_with_the_shipped_default():
    assert re.search(r"^\s*concurrency\s*=\s*5\b", CFG, re.M)


def test_concurrency_drives_both_scopes():
    assert re.search(r"queueSize\s*=.*concurrency", CFG), "queueSize is not derived"
    assert re.search(r"maxForks\s*=.*concurrency", CFG), "maxForks is not derived"


def test_no_per_process_cap_reads_the_bare_param():
    # params.max_forks is null unless explicitly set, so a bare read yields null
    # and Math.min throws at closure-run time -- the silent-resolution failure
    # mode this repo keeps rediscovering.
    assignments = []
    for block in with_name_blocks():
        assignments.extend(re.findall(r"^\s*maxForks\s*=\s*(.+)$", block.body, re.M))
    assert assignments, "expected per-process maxForks overrides in conf/modules.config"
    unbounded = [a for a in assignments if "params.max_forks" not in a]
    assert not unbounded, f"per-process maxForks ignoring params.max_forks: {unbounded}"
    elvis = [a for a in assignments if "?:" in a]
    assert not elvis, (
        f"elvis in a numeric-param fallback: {elvis}. Groovy's 0 is falsy, so `?:` "
        f"cannot express 'unset'. Use (params.max_forks != null ? ... : ...)."
    )
