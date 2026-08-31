"""Every method in synthetic_gt.yaml must have a producer in run_mid.sh.

WHY. `ashlar` sat in synthetic_gt.yaml's `methods` list while run_mid.sh had no way to
produce an ashlar transform: find_transform() globbed <outdir>/<pair>/registered/manifest,
a tree only a PIPELINE run writes, and ashlar stopped being a pipeline backend at
:fire: 6a54479. Every ashlar row of the plan therefore hit the "no transform found" guard
and aborted the run. The guard was right; the producer was missing.

That is a config/driver mismatch, which no unit test of either half can see -- the config
is valid YAML and the driver is valid bash. It needs a test that reads BOTH.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
RUN_MID = REPO / "benchmarks" / "stare_bench" / "run_mid.sh"
CONFIG = REPO / "benchmarks" / "configs" / "synthetic_gt.yaml"


def _methods():
    return list(yaml.safe_load(CONFIG.read_text())["methods"])


def test_every_configured_method_has_a_branch_in_run_mid():
    """A method with no producer aborts the whole plan at its first row."""
    body = RUN_MID.read_text()
    missing = []
    for m in _methods():
        # Each method is reached either by its own `if [[ "$method" == "<m>" ]]` branch
        # (identity, ashlar) or by the pipeline path's --registration_method (tiled, valis).
        own_branch = re.search(rf'\[\[ "\$method" == "{re.escape(m)}" \]\]', body)
        pipeline_backend = m in ("tiled", "valis")
        if not (own_branch or pipeline_backend):
            missing.append(m)
    assert not missing, (
        f"synthetic_gt.yaml lists method(s) run_mid.sh cannot produce a transform for: "
        f"{missing}. Every such row aborts the plan on the 'no transform found' guard."
    )


def test_the_ashlar_branch_drives_the_harness_driver_not_the_pipeline():
    """ashlar must be driven by benchmarks/ashlar/, never by --registration_method.

    The schema enum is ['valis', 'tiled']; --registration_method ashlar is rejected at
    launch. This pins the branch to the two harness entry points that actually exist.
    """
    body = RUN_MID.read_text()
    assert "benchmarks.ashlar.retile" in body, "run_mid.sh does not retile for ashlar"
    assert "benchmarks.ashlar.solve" in body, "run_mid.sh does not solve for ashlar"
    assert "--registration_method \"ashlar\"" not in body
    # find_transform must NOT claim to handle ashlar: that case is unreachable now, and an
    # unreachable case that looks wired is exactly what hid the dead leg.
    assert "tiled|ashlar)" not in body, (
        "find_transform still has a tiled|ashlar case; ashlar returns before reaching it")


def test_the_ashlar_settings_come_from_the_config_not_literals():
    """Hardcoding them in the driver would put the experiment's knobs outside the plan."""
    body = RUN_MID.read_text()
    assert '"$ASHLAR_TILE"' in body and '"$ASHLAR_OVERLAP"' in body
    assert '"$ASHLAR_MAXSHIFT"' in body
    cfg = yaml.safe_load(CONFIG.read_text()).get("ashlar")
    assert cfg, "synthetic_gt.yaml has no ashlar block for run_mid.sh to read"
    assert {"tile_size", "overlap", "maximum_shift_um"} <= set(cfg), sorted(cfg)
