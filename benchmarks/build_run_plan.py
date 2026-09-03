"""Expand a sweep.yaml into a flat run plan (one row per pipeline launch)."""

from __future__ import annotations

import argparse
import csv
import itertools
from pathlib import Path


def _configs(sweep: dict) -> list[tuple[dict, str]]:
    """Build the distinct (params, varied_axis) configurations for a sweep,
    BEFORE replicate expansion. Replicates + ids are added by build_run_plan."""
    strategy = sweep.get("strategy", "ofat")
    axes = sweep.get("axes", {})

    if strategy == "grid":
        keys = list(axes)
        return [
            (dict(zip(keys, combo)), "grid")
            for combo in itertools.product(*(axes[k] for k in keys))
        ]

    # ofat
    baseline = sweep.get("baseline", {})
    configs: list[tuple[dict, str]] = []

    # 1. Input-scale SCALING GRID (full size x channel cross-product), if present.
    #    The (baseline_px, baseline_ch) cell is labelled 'baseline'; the rest
    #    'scaling_grid'. If no grid is defined, emit a single baseline run.
    grid = sweep.get("scaling_grid")
    if grid:
        for tpx in grid["target_px"]:
            for nch in grid["n_channels"]:
                row = dict(baseline, target_px=tpx, n_channels=nch)
                is_base = tpx == baseline.get("target_px") and nch == baseline.get(
                    "n_channels"
                )
                configs.append((row, "baseline" if is_base else "scaling_grid"))
    else:
        configs.append((dict(baseline), "baseline"))

    # 2. REGISTRATION GRID (size x n_register_images x n_channels).
    #    n_register_images is an input-scaling dimension for REGISTER + downstream, so
    #    it's crossed with size. The baseline round count is skipped — the scaling grid
    #    already covers every (size, ch) cell at baseline N. n_channels may be a scalar
    #    or a list ([2, 4]) to benchmark N-image registration at each channel count.
    rgrid = sweep.get("registration_grid")
    if rgrid:
        base_nreg = baseline.get("n_register_images", 2)
        rch = rgrid.get("n_channels", baseline.get("n_channels"))
        rchs = list(rch) if isinstance(rch, (list, tuple)) else [rch]
        for tpx in rgrid["target_px"]:
            for nch in rchs:
                for nreg in rgrid["n_register_images"]:
                    if nreg == base_nreg:
                        continue  # already the scaling-grid cell at baseline N
                    configs.append(
                        (
                            dict(
                                baseline,
                                target_px=tpx,
                                n_channels=nch,
                                n_register_images=nreg,
                            ),
                            "registration_grid",
                        )
                    )

    # 2b. (removed) DISTRIBUTED GRID — the distributed/tiled registration path was archived out of the
    #     pipeline (git tag archive/tiled-valis-2026-07-24); registration is classic REGISTER only, so
    #     there is no distributed counterpart to benchmark. See docs/superpowers/specs/
    #     2026-07-24-benchmark-paper-data-design.md.

    # 3. registration_param_grid IS GONE, folded into registration_method_grid's `valis:` entry.
    #    It crossed memory_mode x reg_micro_reg only, at the baseline reg_max_image_dim, while that
    #    third VALIS knob sat in flat `axes:` and varied at only one (memory_mode, micro) point.
    #    The result was VALIS measured on 11 of its 27 cells against STARE's full 27 -- see the
    #    SYMMETRY RULE block in sweep.yaml. Both methods now go through the one generic per-method
    #    loop below, which is what makes equal dimensionality checkable rather than a convention.

    # 3c. SEGMENTATION GRID — each method benchmarked with ITS OWN parameters. sweep.yaml maps a
    #     seg_method to a dict of {param: [values]}; this pins seg_method and crosses that method's
    #     params (so stardist tiles, cellsam block_size, instantseg tile_size each vary only where they
    #     are live). Labelled segmentation_grid:<method>. cellsam/instantseg need their weights +
    #     container on the cluster.
    sgrid = sweep.get("segmentation_grid")
    if sgrid:
        for method, mparams in sgrid.items():
            keys = list(mparams)
            for combo in itertools.product(*(mparams[k] for k in keys)):
                configs.append(
                    (
                        dict(baseline, seg_method=method, **dict(zip(keys, combo))),
                        f"segmentation_grid:{method}",
                    )
                )

    # 3d. REGISTRATION METHOD GRID — each registration_method benchmarked with ITS OWN parameters,
    #     mirroring segmentation_grid. sweep.yaml maps a registration_method (valis|tiled) to a dict of
    #     {param: [values]}; this pins registration_method and crosses that method's live knobs (VALIS:
    #     memory_mode/reg_micro_reg; STARE/tiled: reg_tiled_*). An OFAT run of a tiled-only knob off the
    #     VALIS baseline would be a no-op, so per-method grids are correct. Labelled
    #     registration_method_grid:<method>. Needs a --paired matrix (a moving panel to register). The
    #     baseline run is the VALIS anchor, so this grid typically only carries the 'tiled' method.
    rmgrid = sweep.get("registration_method_grid")
    if rmgrid:
        for method, mparams in rmgrid.items():
            keys = list(mparams)
            for combo in itertools.product(*(mparams[k] for k in keys)):
                configs.append(
                    (
                        dict(
                            baseline,
                            registration_method=method,
                            **dict(zip(keys, combo)),
                        ),
                        f"registration_method_grid:{method}",
                    )
                )

    # NOTE: a param that is DEAD unless another param enables it must never reach the OFAT loop
    #       below — a flat run off the baseline changes a value the pipeline never reads, so it
    #       silently measures nothing at full run cost. Pin the gate in a per-method grid above
    #       instead. Guarded by test_project_sweep_has_no_dead_axes.

    # 4. OFAT parameter knobs: one config per non-baseline value of each axis,
    #    holding input scale fixed at the baseline cell.
    for axis, values in axes.items():
        for v in values:
            if v == baseline.get(axis):
                continue
            configs.append((dict(baseline, **{axis: v}), axis))
    return configs


def build_run_plan(sweep: dict, repeats: int = 1) -> list[dict]:
    """Expand a sweep into a flat run plan, `repeats` replicate runs per config.

    Each replicate is its own pipeline launch (unique ``run_id`` -> own results
    dir). ``config_id`` groups a config's replicates and ``rep`` is the replicate
    index (0..repeats-1) — the analysis averages metrics within a ``config_id`` to
    report per-config variance. ``repeats=1`` reproduces a single-shot plan.
    """
    if repeats < 1:
        raise ValueError(f"repeats must be >= 1, got {repeats}")
    rows = []
    run_i = 0
    for cfg_i, (params, varied_axis) in enumerate(_configs(sweep)):
        for rep in range(repeats):
            rows.append(
                dict(
                    params,
                    run_id=f"run{run_i:04d}",
                    varied_axis=varied_axis,
                    config_id=f"cfg{cfg_i:03d}",
                    rep=rep,
                )
            )
            run_i += 1
    return rows


def main():
    import yaml

    ap = argparse.ArgumentParser(description="Expand sweep.yaml into run_plan.csv")
    ap.add_argument("--sweep", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="Replicate runs per config (>=3 gives a per-config variance "
        "estimate; timing especially is noisy at n=1). Default: 3.",
    )
    a = ap.parse_args()
    sweep = yaml.safe_load(a.sweep.read_text())
    plan = build_run_plan(sweep, repeats=a.repeats)
    lead = ["run_id", "varied_axis", "config_id", "rep"]
    # Union of keys across ALL rows in first-seen order: some configs carry extra params (e.g. a
    # segmentation_grid method adds its own backend knobs), so keying off plan[0]
    # alone would drop columns and crash DictWriter. A missing value is written blank (restval="") —
    # keep the baseline complete so this stays a safety net, not the norm (blank cells become
    # `--param ""` in run_sweep.sh).
    fields = list(lead)
    for r in plan:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(a.out, "w", newline="") as fh:
        # lineterminator='\n' (not csv's default '\r\n') so run_sweep.sh's bash column
        # parsing doesn't see a trailing '\r' on the last field of each row.
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n", restval="")
        w.writeheader()
        w.writerows(plan)
    n_cfg = len({r["config_id"] for r in plan})
    print(f"Wrote {len(plan)} runs ({n_cfg} configs x {a.repeats} repeats) to {a.out}")


if __name__ == "__main__":
    main()
