#!/usr/bin/env python3
"""Per-patient constrained phenotyping: merged_quant.csv + morphology.csv + model_config.json
-> phenotypes.csv + constraint_audit.csv + phenotype_qc.json (§5)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / "utils"))

from measurements import measurement_key  # noqa: E402
from phenotyping.calibration import (  # noqa: E402
    finalize_calibration,
    marker_calibration_weights,
)
from phenotyping.classify import classify_cells_vectorized  # noqa: E402
from phenotyping.conformal import (  # noqa: E402
    conformal_scores,
    ks_uniform,
    resolve_signs,
)
from phenotyping.crc import (  # noqa: E402
    crc_select_alpha,
    hoeffding_ucb,
    risk_excess_copositivity,
)
from phenotyping.density import compute_density, mondrian_bins  # noqa: E402
from phenotyping.scoring import softmax_pheno_scores  # noqa: E402
from phenotyping.states import state_annotations  # noqa: E402

_SIGN_CHAR = {"pos": "1", "neg": "0", "free": "·", "contra": "x"}
_ALPHA_GRID = [0.005, 0.01, 0.02, 0.05, 0.1, 0.2]


def sign_char(sign: str) -> str:
    return _SIGN_CHAR.get(sign, "·")


def tree_path(name: str, parents: Dict[str, Optional[str]]) -> str:
    if name not in parents:
        return ""
    chain = []
    cur = name
    seen = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        cur = parents.get(cur)
    return "/".join(reversed(chain))


def _marker_values(df: pd.DataFrame, marker: str, spec: dict):
    key = measurement_key(marker, spec["compartment"], spec["statistic"])
    if key in df.columns:
        return df[key].to_numpy(dtype=float), False
    if marker in df.columns:
        return df[marker].to_numpy(dtype=float), False
    return np.zeros(len(df)), True  # missing -> degraded


def run_phenotyping(
    merged_csv, morphology_csv, model_config, *, alpha_target, min_calibration,
    density_c: float = 1.0, max_bins: int = 5,
) -> Tuple[pd.DataFrame, pd.DataFrame, dict]:
    with open(model_config) as fh:
        cfg = json.load(fh)
    lineage = cfg["lineage_markers"]
    states = cfg["state_markers"]
    feasible = cfg["feasible_set"]
    never = cfg["constraints"]["never"]
    requires = cfg["constraints"]["requires"]
    enforce = cfg["constraints"]["enforce"]
    audit = cfg["constraints"]["audit"]
    markers_cfg = cfg["markers"]
    parents = {p["name"]: p["parent"] for p in cfg["phenotypes"]}
    references = {
        m: {"neg_source": markers_cfg[m]["neg_source"], "pos_source": markers_cfg[m]["pos_source"]}
        for m in markers_cfg
    }
    all_names = sorted({e["phenotype"] for e in feasible})

    quant = pd.read_csv(merged_csv)
    morph = pd.read_csv(morphology_csv)[["label", "x", "y"]]
    df = quant.merge(morph, on="label", how="left", suffixes=("", "_morph"))
    n = len(df)
    labels = df["label"].to_numpy()
    centroids = df[["x", "y"]].to_numpy(dtype=float)

    rho, radius = compute_density(centroids, c=density_c)
    bins = mondrian_bins(rho, min_calibration, max_bins=max_bins)
    if len(bins) != n:
        bins = np.zeros(n, dtype=int)

    values: Dict[str, np.ndarray] = {}
    degraded = []
    p_neg: Dict[str, np.ndarray] = {}
    p_pos: Dict[str, np.ndarray] = {}
    calibration_ks: Dict[str, dict] = {}
    for m in list(markers_cfg):
        vals, missing = _marker_values(df, m, markers_cfg[m])
        values[m] = vals
        if missing:
            degraded.append(m)
    for m in list(markers_cfg):
        if m in degraded:
            p_neg[m] = np.ones(n)
            p_pos[m] = np.ones(n)
            continue
        w_neg_m, w_pos_m = marker_calibration_weights(m, values, references)
        cal = finalize_calibration(m, values[m], w_neg_m, w_pos_m, bins, min_calibration)
        if cal.degraded and m not in degraded:
            degraded.append(m)
        p_neg[m], p_pos[m] = conformal_scores(values[m], cal)
        # Fix 4: label-free calibration diagnostic. On confidently-negative
        # cells (high w_neg) p_neg should be ~Uniform(0,1); likewise p_pos on
        # confident positives. KS-to-uniform near 0 = well calibrated; a large
        # value flags the old truncation pathology. Only reported when enough
        # confident reference cells exist to make the KS meaningful.
        if m not in degraded:
            ks_entry = {}
            neg_ref = w_neg_m > 0.9
            pos_ref = w_pos_m > 0.9
            if int(neg_ref.sum()) >= 30:
                ks_entry["neg"] = round(ks_uniform(p_neg[m][neg_ref]), 4)
            if int(pos_ref.sum()) >= 30:
                ks_entry["pos"] = round(ks_uniform(p_pos[m][pos_ref]), 4)
            if ks_entry:
                calibration_ks[m] = ks_entry

    def classify_all(alpha):
        sm = {m: resolve_signs(p_neg[m], p_pos[m], alpha) for m in lineage}
        outs = classify_cells_vectorized(sm, feasible, never, requires, lineage)
        return sm, outs

    crc_ran = bool(audit)
    if crc_ran:
        audit_markers = sorted({mk for c in audit for mk in c["markers"]})
        nominal = {c["id"]: c["r"] for c in audit}

        def risk_ucb(alpha):
            sm, outs = classify_all(alpha)
            committed = np.array(
                [i for i, o in enumerate(outs) if len(o.candidate_names) == 1], dtype=int
            )
            cs = {
                mk: (sm[mk][committed] == "pos").astype(int) if committed.size else np.array([], dtype=int)
                for mk in audit_markers
            }
            risk, nc = risk_excess_copositivity(cs, audit, nominal)
            return hoeffding_ucb(risk, nc)

        chosen_alpha = crc_select_alpha(_ALPHA_GRID, risk_ucb, alpha_target)
    else:
        chosen_alpha = alpha_target

    sm, outs = classify_all(chosen_alpha)

    # state markers
    state_signs = {}
    for m in states:
        state_signs[m] = resolve_signs(p_neg[m], p_pos[m], chosen_alpha)
    state_ann = {m: state_annotations(state_signs[m]) for m in states}

    rows = []
    for i in range(n):
        o = outs[i]
        signs_row = {m: sm[m][i] for m in lineage}
        pneg_row = {m: float(p_neg[m][i]) for m in lineage}
        ppos_row = {m: float(p_pos[m][i]) for m in lineage}
        ps = softmax_pheno_scores(
            o.candidate_patterns, all_names, pneg_row, ppos_row, signs_row, enforce, lineage
        )
        row = {
            "label": labels[i],
            "phenotype": o.outcome,
            "candidates": ";".join(o.candidate_names),
            "n_candidates": len(o.candidate_names),
            "tree_path": tree_path(o.outcome, parents),
            "density_bin": int(bins[i]),
            "outcome": o.outcome,
            "empty_type": o.empty_type,
            "violated_constraint_id": o.violated_constraint_id,
            "provenance": 0,
        }
        for name in all_names:
            row[f"pheno_score:{name}"] = round(float(ps.get(name, 0.0)), 6)
        for m in lineage + states:
            row[f"p_neg:{m}"] = round(float(p_neg[m][i]), 6)
        for m in lineage + states:
            row[f"p_pos:{m}"] = round(float(p_pos[m][i]), 6)
        for m in lineage:
            row[f"sign:{m}"] = sign_char(sm[m][i])
        for m in states:
            row[f"state:{m}"] = int(state_ann[m][i])
        rows.append(row)

    columns = (
        ["label", "phenotype", "candidates", "n_candidates", "tree_path", "density_bin",
         "outcome", "empty_type", "violated_constraint_id", "provenance"]
        + [f"pheno_score:{name}" for name in all_names]
        + [f"p_neg:{m}" for m in lineage + states]
        + [f"p_pos:{m}" for m in lineage + states]
        + [f"sign:{m}" for m in lineage]
        + [f"state:{m}" for m in states]
    )
    pheno_df = pd.DataFrame(rows, columns=columns)

    # constraint audit table
    committed_mask = pheno_df["n_candidates"] == 1
    audit_rows = []
    for c in audit:
        a, b = c["markers"]
        va = (pheno_df.loc[committed_mask, f"sign:{a}"] == "1").to_numpy() if a in lineage else np.array([])
        vb = (pheno_df.loc[committed_mask, f"sign:{b}"] == "1").to_numpy() if b in lineage else np.array([])
        nboth = int(np.sum(va & vb)) if va.size else 0
        obs = (nboth / va.size) if va.size else 0.0
        rho_c = rho[committed_mask.to_numpy()]
        copos = (va & vb).astype(float) if va.size else np.array([])
        dens_corr = float(np.corrcoef(rho_c, copos)[0, 1]) if copos.size and copos.std() > 0 else 0.0
        verdict = (
            "SEGMENTATION (spillover)" if dens_corr > 0.5
            else "REVIEW: raise r or rate rare->never" if obs > c["r"]
            else "OK"
        )
        audit_rows.append({
            "id": c["id"], "markers": f"{a}|{b}", "observed": round(obs, 4),
            "nominal": c["r"], "density_corr": round(dens_corr, 4),
            "neighbour_contact_corr": round(dens_corr, 4), "verdict": verdict,
        })
    audit_df = pd.DataFrame(
        audit_rows,
        columns=["id", "markers", "observed", "nominal", "density_corr", "neighbour_contact_corr", "verdict"],
    )

    qc = {
        "chosen_alpha": chosen_alpha,
        "alpha_target": alpha_target,
        "crc_ran": crc_ran,
        "reporting_mode": not crc_ran,
        "degraded_markers": sorted(set(degraded)),
        "n_cells": n,
        "density_radius": radius,
        "n_bins": int(len(np.unique(bins))),
        "calibration_ks": calibration_ks,
    }
    return pheno_df, audit_df, qc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Per-patient constrained phenotyping")
    ap.add_argument("--merged_quant", required=True)
    ap.add_argument("--morphology", required=True)
    ap.add_argument("--model_config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--audit", required=True)
    ap.add_argument("--qc", required=True)
    ap.add_argument("--alpha-target", type=float, default=0.05)
    ap.add_argument("--min-calibration", type=int, default=50)
    args = ap.parse_args(argv)

    pheno_df, audit_df, qc = run_phenotyping(
        args.merged_quant, args.morphology, args.model_config,
        alpha_target=args.alpha_target, min_calibration=args.min_calibration,
    )
    pheno_df.to_csv(args.out, index=False)
    audit_df.to_csv(args.audit, index=False)
    with open(args.qc, "w") as fh:
        json.dump(qc, fh, indent=2)
    for m in qc["degraded_markers"]:
        print(f"[WARN] marker {m} degraded to undetermined (calibration failed)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
