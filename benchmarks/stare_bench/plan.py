"""The committed experiment plan -- written BEFORE any method is run.

TEST-SET DISCIPLINE. Seeds, correlation lengths, amplitudes and blank
fractions are fixed here and committed. Re-tuning them after seeing results
would turn the benchmark into a search for a flattering configuration.

SWEEP DENSITY. Eleven points, 0.00 to 1.00 by 0.10. The earlier
[0, .25, .5, .75, 1.0] sweep placed ZERO samples inside the 40-70%-blank band
where tiles are accepted with a wrong displacement; this places four. Eleven is
a subset of the 21-point grid tests/test_tile_residual_confidence.py uses, so
the two remain directly comparable at roughly half the cost.

AMPLITUDE AXIS. Two values, 12.0 and 48.0 px. A unit-rung measurement on the
real driver found STARE scoring essentially at the identity baseline
(ratio(max) = 0.998) because its COARSE stage absorbs the field's lowest
frequency into a global affine and `bin/tiled_solve.py` only refines control
points whose rigid-stage TRE exceeds `reg_tiled_gate_tre` (default 1.0px): at
low total displacement the post-affine residual is sub-pixel, the gate zeroes
it, and the mesh collapses to identity. 12.0 sits near that gate (the
interesting, arguably deficient regime); 48.0 sits well above it (the mesh has
real work to do). Both are reported rather than tuned away.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .fields import make_field

__all__ = ["BLANK_FRACTIONS", "build_plan", "write_plan"]

BLANK_FRACTIONS = tuple(round(i / 10.0, 2) for i in range(11))


def _validate_fields_are_independent(config):
    """Dry-run ``make_field`` for every committed (family, correlation_px) at
    the committed ``size``, so a rigged sweep fails at plan-build time rather
    than reading as validated.

    This replaces the former ``corr % REG_TILED_TILE == 0 or REG_TILED_TILE %
    corr == 0`` rule. The design spec
    (``docs/superpowers/specs/2026-08-26-stare-synthetic-benchmark-design.md``,
    "Knot-spacing independence") shows that rule is neither necessary nor
    sufficient: ``correlation_px=16000`` at 51200px passes it while yielding a
    pitch of 2047.96 (colliding with STARE's grid), and ``correlation_px=2048``
    at 4096px fails it while yielding a perfectly safe pitch of 273. The
    quantity that actually matters is the DERIVED coarse-grid pitch, and
    ``fields.make_field`` already raises ``ValueError`` when that pitch is not
    safely below STARE's control-grid spacing (``MAX_COARSE_PITCH_PX``) --
    this just calls the real check here instead of duplicating (and
    drifting from) its rule.

    Only ``"random_fourier"`` accepts a ``correlation_px``/``amplitude_px``
    pair -- the other field families (``affine``, ``adversarial``) have no
    coarse grid and are not part of this firewall, so they are skipped here
    exactly as they are in the real ``generate_pair`` call.
    """
    size = tuple(config["size"])
    for family in config["field_families"]:
        if family != "random_fourier":
            continue
        for corr in config["correlation_px"]:
            # amplitude_px does not affect the derived pitch (see
            # fields._random_fourier); any fixed value dry-runs the same check
            # real generation performs.
            make_field(family, size, seed=0, correlation_px=corr, amplitude_px=1.0)


def build_plan(config):
    """Full cross-product of seeds x families x correlations x amplitudes x
    blanking x methods."""
    _validate_fields_are_independent(config)

    records = []
    for seed in config["seeds"]:
        for family in config["field_families"]:
            for corr in config["correlation_px"]:
                for amplitude in config["amplitude_px"]:
                    for blank in BLANK_FRACTIONS:
                        # Scale-then-round-then-truncate (mirroring
                        # blank_fraction's `int(blank * 100)`), NOT a bare
                        # `int(...)` truncation: a bare int() collapses
                        # amplitude/correlation values that share an integer
                        # floor (e.g. 12.0 and 12.5 both -> "012") into one
                        # pair_id, silently pairing two different conditions
                        # to the same generated image. x10 preserves one
                        # decimal place, which is finer than any committed
                        # value needs.
                        corr_tag = int(round(corr * 10))
                        amp_tag = int(round(amplitude * 10))
                        pair_id = (
                            f"s{seed}_{family}_c{corr_tag:05d}_a{amp_tag:04d}"
                            f"_b{int(blank * 100):03d}"
                        )
                        for method in config["methods"]:
                            records.append({
                                "run_id": f"{pair_id}_{method}",
                                "pair_id": pair_id,
                                "method": method,
                                "seed": seed,
                                "field_family": family,
                                "correlation_px": corr,
                                "amplitude_px": amplitude,
                                "blank_fraction": blank,
                                "size": list(config["size"]),
                                "varied_axis": "synthetic_gt",
                            })
    return records


def write_plan(records, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = list(records[0])
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for r in records:
            writer.writerow({k: json.dumps(v) if isinstance(v, list) else v
                             for k, v in r.items()})
