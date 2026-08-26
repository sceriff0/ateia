"""Turn metric dicts into the registration_synthetic_gt.csv contract.

WHY A SEPARATE TABLE from registration_accuracy.csv: that one is the
landmark-free seg-overlap signal at grain (run, moving slide, stage), and it is
bounded by nucleus diameter (~5-15 um). This table carries EPE, gate ROC and
Jacobian columns that have no meaning there. Merging them would put a bounded
metric and an exact sub-pixel one in the same column space. It also cannot
aggregate across `stage`: the backends do not share a stage vocabulary (VALIS
native/rigid/non_rigid/micro vs. the manifest backends' native/rigid/refined).

THE HARD RULE THIS MODULE ENFORCES: no metric STARE optimises may appear as
STARE's accuracy. `bin/tiled_solve.py`'s "intrinsic TRE" is built from the
same per-tile phase correlations the registration itself optimises, so it is
evidence of convergence, never of accuracy. It is admitted only under a
`diag_` prefix, and accuracy_row REFUSES a metric dict that smuggles it in
elsewhere.
"""

from __future__ import annotations

import csv
from pathlib import Path

__all__ = [
    "ACCURACY_COLUMNS",
    "accuracy_row",
    "write_accuracy_csv",
    "FORBIDDEN_IN_ACCURACY",
]

# Keys that name a self-reported (circular) quantity. They may only reach the
# table through the explicit `intrinsic_tre` argument, which lands under diag_.
# final_p50_px/final_p90_px/rigid_p90_px are the spellings
# benchmarks/registration_eval/eval_tre.py:read_stare_tre re-flattens the SAME
# circular STARE post-mesh residual under -- same quantity, different label.
FORBIDDEN_IN_ACCURACY = {
    "intrinsic_tre",
    "stare_tre",
    "coarse_tre_px",
    "rigid_p50_px",
    "residual_after_px",
    "final_p50_px",
    "final_p90_px",
    "rigid_p90_px",
}

# epe_stats keys accuracy_row's own output depends on -- unlike the "genuinely
# optional" .get()-based fields elsewhere in this module (intrinsic_tre; the
# field_params sub-keys some field families don't define), a missing one of
# these must fail loudly rather than write a silent None into an audit column
# whose entire purpose is telling a reader whether percentiles are exact.
_REQUIRED_EPE_KEYS = {
    "mean_px", "median_px", "p95_px", "max_px", "n", "subsample_effective",
}

ACCURACY_COLUMNS = [
    "run_id", "method", "pair_id",
    "generator_version", "seed", "param_hash", "field_family",
    "field_correlation_px", "field_amplitude_px",
    "epe_mean_px", "epe_median_px", "epe_p95_px", "epe_max_px", "epe_n",
    "epe_subsample_effective",
    "gate_precision", "gate_recall", "gate_f1", "gate_auc",
    "gate_tp", "gate_fp", "gate_tn", "gate_fn",
    "folding_rate", "det_min", "det_p05", "det_median",
    "lipschitz", "lipschitz_converges",
    "tre_median_px", "tre_mean_px", "tre_p90_px", "tre_median_rtre", "tre_n",
    "diag_intrinsic_tre_px",
]


def _check_clean(*dicts):
    for d in dicts:
        bad = FORBIDDEN_IN_ACCURACY & set(d or {})
        if bad:
            raise ValueError(
                f"{sorted(bad)} is a self-reported (diagnostic) quantity and "
                "cannot enter an accuracy column; pass it as intrinsic_tre"
            )


def _check_required(d, required, label):
    missing = sorted(required - set(d or {}))
    if missing:
        raise ValueError(f"{label} is missing required keys: {missing}")


def accuracy_row(*, run_id, method, pair_id, truth, epe_stats, jac_stats,
                  lip_stats, tre_summary, gate_stats=None, gate_auc_value=None,
                  intrinsic_tre=None):
    """One row of registration_synthetic_gt.csv.

    ``gate_stats``/``gate_auc_value`` are explicitly ``None``-able: an
    UNMEASURED gate (no per-tile accept/reject data available -- e.g. a
    caller scoring an externally-supplied transform that carries no control
    points) must write ``None`` into every ``gate_*`` column, not a value
    computed from an empty accept/score mapping. ``gate_roc({}, {})`` on real
    labels does not error and does not come back empty -- ``gate_recall``
    lands on a literal ``0.0`` (every truly-registrable tile counts as a
    false negative) and ``gate_auc`` on a literal ``0.5`` (its own
    documented tie-broken value for a constant, uninformative score) -- two
    entirely fabricated, plausible-looking numbers. A caller with no gate
    data must pass ``gate_stats=None, gate_auc_value=None`` here rather than
    routing empty dicts through ``gate_roc``/``gate_auc`` itself.
    """
    _check_clean(epe_stats, gate_stats, jac_stats, lip_stats, tre_summary)
    _check_required(epe_stats, _REQUIRED_EPE_KEYS, "epe_stats")
    fp = truth.get("field_params", {})
    gs = gate_stats or {}
    return {
        "run_id": run_id,
        "method": method,
        "pair_id": pair_id,
        "generator_version": truth.get("generator_version"),
        "seed": truth.get("seed"),
        "param_hash": truth.get("param_hash"),
        "field_family": truth.get("field_family"),
        "field_correlation_px": fp.get("correlation_px"),
        "field_amplitude_px": fp.get("amplitude_px"),
        "epe_mean_px": epe_stats.get("mean_px"),
        "epe_median_px": epe_stats.get("median_px"),
        "epe_p95_px": epe_stats.get("p95_px"),
        "epe_max_px": epe_stats.get("max_px"),
        "epe_n": epe_stats.get("n"),
        "epe_subsample_effective": epe_stats.get("subsample_effective"),
        "gate_precision": gs.get("precision"),
        "gate_recall": gs.get("recall"),
        "gate_f1": gs.get("f1"),
        "gate_auc": gate_auc_value,
        "gate_tp": gs.get("tp"),
        "gate_fp": gs.get("fp"),
        "gate_tn": gs.get("tn"),
        "gate_fn": gs.get("fn"),
        "folding_rate": jac_stats.get("folding_rate"),
        "det_min": jac_stats.get("det_min"),
        "det_p05": jac_stats.get("det_p05"),
        "det_median": jac_stats.get("det_median"),
        "lipschitz": lip_stats.get("lipschitz"),
        "lipschitz_converges": lip_stats.get("converges"),
        "tre_median_px": tre_summary.get("median_px"),
        "tre_mean_px": tre_summary.get("mean_px"),
        "tre_p90_px": tre_summary.get("p90_px"),
        "tre_median_rtre": tre_summary.get("median_rtre"),
        "tre_n": tre_summary.get("n"),
        "diag_intrinsic_tre_px": intrinsic_tre,
    }


def write_accuracy_csv(rows, path):
    """Write rows in ACCURACY_COLUMNS order, always including the header.

    Every row must carry EXACTLY the ACCURACY_COLUMNS keys -- no more, no
    fewer. Building `{k: row.get(k) for k in ACCURACY_COLUMNS}` before
    handing a row to `csv.DictWriter` would silently drop an unexpected extra
    key and silently blank a missing one, defeating DictWriter's own
    `extrasaction='raise'` default. Validate explicitly instead.

    `rows` is materialised into a list FIRST: the validation pass and the
    write pass below both walk it, so a one-shot iterable (a generator
    expression is the natural way to call this) would be exhausted by
    validation alone, leaving the write pass nothing to see and producing a
    silently header-only CSV.
    """
    rows = list(rows)
    columns = set(ACCURACY_COLUMNS)
    for row in rows:
        keys = set(row)
        extra = sorted(keys - columns)
        missing = sorted(columns - keys)
        if extra or missing:
            raise ValueError(
                f"row does not match ACCURACY_COLUMNS: extra={extra} "
                f"missing={missing}"
            )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=ACCURACY_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
