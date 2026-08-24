"""Expand arms.yaml into a real-sample arm plan (one row per pipeline launch).

The counterpart to build_run_plan.py, for the REAL slides rather than the
synthetic matrix. Two outputs, because the sweep and the consumer want different
things from the same expansion:

    arm_plan.csv   one row per pipeline launch, consumed by run_arms.sh
    arms.csv       the LABEL manifest ihc_method's registration_arms.R reads
                   (arm_dir, backend, memory_mode, micro_reg, label)

arms.csv is written into the results root, not beside the plan, because that is
where the consumer looks: `data/registration_arms/arms.csv`.

WHY THE LABEL MANIFEST IS NOT OPTIONAL HERE. registration_arms.R falls back to
parsing the directory name for `high`/`low` and a micro depth when arms.csv is
absent. A mislabelled arm does not fail — it produces a clean figure with the
conclusion inverted. The QC-segmenter-crossed arms below (`valis_high_micro2_seg
stardist`) are exactly the names that fallback would read wrong, so this module
always emits the manifest rather than leaving it to a flag.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

# Params that exist only on one backend. A tiled arm must carry NEITHER, and the
# consumer requires them blank so it can tell "not applicable" from "at default".
# Writing memory_mode=high on a STARE arm would invent a value the run never had.
VALIS_ONLY = ("memory_mode", "reg_micro_reg")

# The mirror of VALIS_ONLY for the ashlar backend. Same rule, same reason: a non-ashlar arm
# must carry these BLANK so the consumer can tell "not applicable" from "at default", and so
# the launcher never passes a param the run has no use for.
ASHLAR_ONLY = ("reg_ashlar_tile", "reg_ashlar_overlap", "reg_ashlar_max_shift_um")


def valis_arm_name(memory_mode: str, micro: int) -> str:
    return f"valis_{memory_mode}_micro{micro}"


def ashlar_arm_name(tile: int) -> str:
    # The tile size is IN the name, not only in arms.csv. registration_arms.R falls back to
    # parsing the directory name when the manifest is missing, and two ashlar arms that
    # differed only by a column it failed to read would render as one duplicated box.
    return f"ashlar_tile{tile}"


def _registration_arms(cfg: dict) -> list[dict]:
    """The registration arms, before the QC-segmenter cross is applied."""
    arms: list[dict] = []
    ra = cfg.get("registration_arms", {})

    valis = ra.get("valis") or {}
    for mm in valis.get("memory_mode", []):
        for micro in valis.get("reg_micro_reg", []):
            arms.append({
                "arm": valis_arm_name(mm, micro),
                "backend": "valis",
                "memory_mode": mm,
                "reg_micro_reg": micro,
                "label": f"{mm} / micro {micro}",
            })

    # ASHLAR: a third backend, so like tiled it carries neither VALIS-only param. It fans out
    # over tile_size because that is ashlar's fairness knob against STARE's reg_tiled_tile --
    # ashlar takes one independent shift per tile, so grid granularity changes how much local
    # freedom it has, not just what it costs.
    ashlar = ra.get("ashlar") or {}
    if ashlar.get("enabled"):
        for tile in ashlar.get("tile_size", []):
            arms.append({
                "arm": ashlar_arm_name(tile),
                "backend": "ashlar",
                "memory_mode": "",
                "reg_micro_reg": "",
                "reg_ashlar_tile": tile,
                "reg_ashlar_overlap": ashlar.get("overlap_fraction", ""),
                "reg_ashlar_max_shift_um": ashlar.get("maximum_shift_um", ""),
                "label": f"ashlar (tile {tile})",
            })

    tiled = ra.get("tiled") or {}
    if tiled.get("enabled"):
        # No memory_mode, no reg_micro_reg — see VALIS_ONLY. The consumer keys
        # "is this the tiled backend" off the `backend` column when arms.csv is
        # present, and off the substring `tiled`/`stare` when it is not; the name
        # satisfies both so the fallback path stays correct too.
        arms.append({
            "arm": "tiled_defaults",
            "backend": "tiled",
            "memory_mode": "",
            "reg_micro_reg": "",
            "label": "tiled (STARE, defaults)",
        })
    return arms


def _apply_qc_segmenter_cross(arms: list[dict], cfg: dict) -> list[dict]:
    """Cross the registration arms with the QC segmenter (params.seg_method).

    seg_qc.nf segments the native slides with the RUN'S OWN segmenter, so this
    changes which nuclei the registration accuracy is measured on while leaving
    the registration itself byte-identical. A robustness axis on the measuring
    instrument, never a quality claim about registration.
    """
    x = cfg.get("qc_segmenter_cross") or {}
    mode = x.get("cross", "none")
    methods = list(x.get("seg_method", []))
    default = (cfg.get("baseline") or {}).get("seg_method")

    # Every arm carries the baseline segmenter unless it is an extra cross arm.
    for a in arms:
        a.setdefault("seg_method", default)

    if mode == "none" or not methods:
        return arms

    if mode == "reference":
        ref = x.get("reference_arm")
        names = {a["arm"] for a in arms}
        if ref not in names:
            raise ValueError(
                f"qc_segmenter_cross.reference_arm={ref!r} is not an arm this "
                f"config produces. Available: {sorted(names)}")
        targets = [a for a in arms if a["arm"] == ref]
    elif mode == "all":
        targets = list(arms)
    else:
        raise ValueError(
            f"qc_segmenter_cross.cross must be 'reference', 'all' or 'none', "
            f"got {mode!r}")

    extra: list[dict] = []
    for base in targets:
        for m in methods:
            if m == base["seg_method"]:
                continue          # that IS the base arm; a duplicate run measures nothing
            a = dict(base)
            a["arm"] = f"{base['arm']}_seg{m}"
            a["seg_method"] = m
            a["label"] = f"{base['label']} [QC seg: {m}]"
            extra.append(a)
    return arms + extra


# The one shared preprocessing run every registration arm resumes from.
PREPROCESS_ARM = "preprocess_shared"


def _compute_arm_name(patient: str, rep: int) -> str:
    return f"compute_{patient or 'all'}" + (f"_rep{rep}" if rep else "")


_LABELS: dict[str, str] = {}


def build_arm_plan(cfg: dict) -> list[dict]:
    """Expand arms.yaml into a flat list of pipeline launches.

    Three arm kinds, deliberately FACTORED rather than crossed:

      registration  --start preprocessing --stop registration, one per config.
      segmentation  --start segmentation, resuming ONE registration arm's
                    csv/registered.csv, so registration is paid for once.
      compute       the full pipeline, traced, for the per-process cost profile.
    """
    baseline = cfg.get("baseline") or {}
    if baseline.get("reg_qc") != 2:
        # Not a style preference: reg_qc<2 emits no staged seg-overlap QC, so
        # every registration arm would produce zero accuracy rows and the
        # consumer would render an empty page with no error.
        raise ValueError(
            "arms.yaml baseline.reg_qc must be 2 — the registration arm ranking "
            f"reads the reg_qc=2 staged QC and nothing else emits it (got "
            f"{baseline.get('reg_qc')!r})")

    rows: list[dict] = []
    n = 0
    _LABELS.clear()

    # PREPROCESSING IS SHARED. Nothing in registration_arms or qc_segmenter_cross
    # touches a preproc_* param, so all 9 registration arms would otherwise re-run
    # an identical (and, on a real WSI, expensive) preprocessing step. Run it once
    # and have every registration arm resume from its csv/preprocessed.csv -- the
    # same factoring already applied to the segmentation arms.
    rows.append({
        "run_id": PREPROCESS_ARM, "arm_kind": "preprocess",
        "start": "preprocessing", "stop": "preprocessing",
        "from_arm": "", "from_csv": "", "rep": 0,
        "arm": PREPROCESS_ARM, "backend": "", "memory_mode": "",
        "reg_micro_reg": "", "seg_method": baseline.get("seg_method", ""),
        "registration_method": "", "reg_qc": "",
    })
    n += 1

    arms = _apply_qc_segmenter_cross(_registration_arms(cfg), cfg)
    for a in arms:
        _LABELS[a["arm"]] = a["label"]
        rows.append({
            "run_id": a["arm"], "arm_kind": "registration",
            "start": "registration", "stop": "registration",
            "from_arm": PREPROCESS_ARM, "from_csv": "preprocessed", "rep": 0,
            "registration_method": a["backend"],
            **{k: a[k] for k in ("arm", "backend", "memory_mode",
                                 "reg_micro_reg", "seg_method")},
            # Blank on every non-ashlar arm, exactly as memory_mode/reg_micro_reg are blank
            # on non-VALIS arms: run_arms.sh's add_param skips an empty value, so a VALIS arm
            # never receives --reg_ashlar_tile "" (which schema validation would reject).
            **{k: a.get(k, "") for k in ASHLAR_ONLY},
            "reg_qc": 2,
        })
        n += 1

    sa = cfg.get("segmentation_arms") or {}
    if sa:
        frm = sa.get("from_arm")
        if frm not in {a["arm"] for a in arms}:
            raise ValueError(
                f"segmentation_arms.from_arm={frm!r} names no registration arm. "
                f"It must be an arm this plan runs, because the segmentation arms "
                f"resume from its csv/registered.csv. Available: "
                f"{sorted({a['arm'] for a in arms})}")
        for m in sa.get("seg_method", []):
            rows.append({
                "run_id": f"seg_{m}", "arm_kind": "segmentation",
                "start": "segmentation", "stop": "", "from_arm": frm,
                "from_csv": "registered", "rep": 0,
                "arm": f"seg_{m}", "backend": "", "memory_mode": "",
                "reg_micro_reg": "", "seg_method": m,
                "registration_method": "",
                "reg_qc": "",
            })
            n += 1

    cp = cfg.get("compute_profile")
    if cp is not None:
        pats = cp.get("patients") or [""]        # "" = every patient in the input.csv
        for p in pats:
            for rep in range(int(cp.get("repeats", 1) or 1)):
                rows.append({
                    "run_id": _compute_arm_name(p, rep), "arm_kind": "compute",
                    "start": "", "stop": "", "from_arm": "", "from_csv": "",
                    "rep": rep,
                    "arm": _compute_arm_name(p, rep),
                    "backend": baseline.get("registration_method", "valis"),
                    "memory_mode": baseline.get("memory_mode", ""),
                    "reg_micro_reg": baseline.get("reg_micro_reg", ""),
                    "seg_method": baseline.get("seg_method", ""),
                    "registration_method": baseline.get("registration_method", "valis"),
                    "reg_qc": baseline.get("reg_qc", 2),
                    "only_patient": p,
                })
                n += 1
    return rows


def arms_manifest_rows(plan: list[dict]) -> list[dict]:
    """The consumer's arms.csv: registration arms only, one row per arm dir.

    Segmentation and compute arms are deliberately absent — registration_arms.R
    ranks REGISTRATION configurations, and a segmentation arm listed there would
    appear as an unlabelled box in every panel.
    """
    return [{
        "arm_dir": r["arm"],
        "backend": r["backend"],
        "memory_mode": r["memory_mode"],
        "micro_reg": r["reg_micro_reg"],
        "label": _LABELS[r["arm"]],
    } for r in plan if r["arm_kind"] == "registration"]


def schema_enums(schema_path: Path) -> dict:
    """param -> allowed values, from nextflow_schema.json.

    The same enum plugin/nf-schema's validateParameters() enforces at run time.
    """
    import json

    out: dict[str, list] = {}

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, dict) and "enum" in v:
                    out[k] = v["enum"]
                walk(v)
        elif isinstance(node, list):
            for i in node:
                walk(i)

    walk(json.loads(schema_path.read_text()))
    return out


def validate_against_schema(plan: list[dict], schema_path: Path) -> list[str]:
    """Every enum-valued param an arm pins must be a value the pipeline accepts.

    This exists because a one-character typo cost a whole submission: arms.yaml
    pinned `seg_method: instanseg` (one 't'), which is NOT the pipeline's
    `instantseg`, and nothing caught it until all 14 runs had been queued,
    scheduled, and died inside validateParameters(). The name-level checks all
    passed -- and the typo is easy, because `instanseg_model_dir` really is
    spelled with one 't'.

    Checked HERE, in the plan builder, so it fails in the seconds-long local step
    rather than after the cluster has accepted the work.
    """
    enums = schema_enums(schema_path)
    bad = []
    for r in plan:
        for k, v in r.items():
            if k in enums and v not in ("", None) and v not in enums[k]:
                bad.append(f"  {r['arm']}: {k}={v!r} -- allowed: {enums[k]}")
    return bad


def _write_csv(path: Path, rows: list[dict], lead: list[str]) -> None:
    fields = list(lead)
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        # lineterminator='\n' (not csv's '\r\n') so run_arms.sh's bash column
        # parsing never sees a trailing '\r' on the last field.
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n", restval="")
        w.writeheader()
        w.writerows(rows)


def read_input_patients(input_csv: Path) -> list[str]:
    """patient_id values in the real samplesheet, first-seen order, de-duplicated."""
    with open(input_csv, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows or "patient_id" not in (rows[0] or {}):
        raise ValueError(f"{input_csv} has no patient_id column")
    seen: list[str] = []
    for r in rows:
        p = (r.get("patient_id") or "").strip()
        if p and p not in seen:
            seen.append(p)
    return seen


def main():
    import yaml

    ap = argparse.ArgumentParser(
        description="Expand arms.yaml into arm_plan.csv (+ the consumer's arms.csv)")
    ap.add_argument("--arms", required=True, type=Path)
    ap.add_argument("--input", required=True, type=Path,
                    help="the REAL samplesheet (patient_id,path_to_file,is_reference,channels)")
    ap.add_argument("--out", required=True, type=Path, help="arm_plan.csv")
    ap.add_argument("--results-root", type=Path, default=None,
                    help="where arms.csv is written (default: alongside --out). "
                         "Point it at the results root the runs publish into — that "
                         "is where registration_arms.R looks for it.")
    a = ap.parse_args()

    cfg = yaml.safe_load(a.arms.read_text())
    plan = build_arm_plan(cfg)

    # Fail before anything is written, so a stale plan is never left behind for
    # the launcher's non-empty check to accept.
    schema = Path(__file__).resolve().parents[1] / "nextflow_schema.json"
    if schema.exists():
        bad = validate_against_schema(plan, schema)
        if bad:
            raise SystemExit(
                "arms.yaml pins values the pipeline will reject:\n"
                + "\n".join(bad)
                + "\n\nThese are checked against nextflow_schema.json here because "
                  "validateParameters() would otherwise only reject them once every "
                  "run had been queued and scheduled.")

    patients = read_input_patients(a.input)
    cp = cfg.get("compute_profile") or {}
    unknown = [p for p in (cp.get("patients") or []) if p not in patients]
    if unknown:
        raise SystemExit(
            f"compute_profile.patients names patients absent from {a.input}: "
            f"{unknown}\nPresent: {patients}")

    _write_csv(a.out, plan, ["run_id", "arm_kind", "arm"])
    root = a.results_root or a.out.parent
    _write_csv(root / "arms.csv", arms_manifest_rows(plan),
               ["arm_dir", "backend", "memory_mode", "micro_reg", "label"])

    by_kind: dict[str, int] = {}
    for r in plan:
        by_kind[r["arm_kind"]] = by_kind.get(r["arm_kind"], 0) + 1
    kinds = ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items()))
    print(f"Wrote {len(plan)} arm runs ({kinds}) to {a.out}")
    print(f"Wrote label manifest to {root / 'arms.csv'}")
    print(f"{len(patients)} patient(s) in {a.input}: {', '.join(patients)}")
    # Say the multiplier out loud. Every registration/segmentation arm runs the
    # WHOLE cohort, so the launch count is not the run count.
    per_cohort = sum(1 for r in plan if r["arm_kind"] != "compute")
    print(f"NOTE: {per_cohort} of these launch the full cohort "
          f"({per_cohort} x {len(patients)} patient-runs), plus "
          f"{by_kind.get('compute', 0)} compute-profile launch(es).")


if __name__ == "__main__":
    main()
