"""Guards for the REAL-SAMPLE arm plan (benchmarks/build_arm_plan.py + arms.yaml).

The sweep's guards (test_build_run_plan.py) tie sweep.yaml to nextflow.config.
These tie arms.yaml to two things the sweep does not touch: the pipeline's own
param names, and the CONSUMER contract in ihc_method/code/registration_arms.R.
A drift on either side fails silently in the same way — a page that renders
cleanly with the wrong answer — so each is pinned here rather than reviewed.
"""
from __future__ import annotations

import csv
import importlib.util
import io
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

BENCH = Path(__file__).parents[1]
REPO_ROOT = BENCH.parent
ARMS_YAML = BENCH / "configs" / "arms.yaml"

from benchmarks.build_arm_plan import (
    arms_manifest_rows,
    build_arm_plan,
    read_input_patients,
)


def _param_checker():
    path = REPO_ROOT / "tests" / "check_param_consistency.py"
    # That module imports its sibling `_code_view` flat; loading a file by path does
    # NOT put its directory on sys.path, so the sibling import raises
    # ModuleNotFoundError before a single assertion runs. Same fix as
    # test_build_run_plan.py's loader.
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("_check_param_consistency", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cfg() -> dict:
    return yaml.safe_load(ARMS_YAML.read_text())


@pytest.fixture(scope="module")
def plan(cfg) -> list[dict]:
    return build_arm_plan(cfg)


# ---------------------------------------------------------------------------
# The pipeline contract
# ---------------------------------------------------------------------------

def test_arms_baseline_params_all_exist_in_the_pipeline(cfg):
    """Every param arms.yaml pins must be a real nextflow.config param.

    The same failure the sweep's baseline guard closes: a renamed or removed
    param leaves an arm pinning a value the pipeline never reads, so the run
    costs full price and measures the default instead of the arm.
    """
    defaults = _param_checker().extract_config_defaults(
        (REPO_ROOT / "nextflow.config").read_text())
    unknown = sorted(set(cfg["baseline"]) - set(defaults))
    assert not unknown, (
        f"arms.yaml baseline pins params absent from nextflow.config: {unknown}")


def test_every_flag_run_arms_passes_is_a_real_pipeline_param():
    """The flags run_arms.sh actually passes must all exist in nextflow.config.

    Derived from the script's own `add_param <name>` lines rather than from the
    plan's columns: the plan also carries bookkeeping columns (arm, backend,
    from_arm) that are never passed as flags, so checking columns would either
    fail on those or need an exclusion list that silently grows. Parsing the
    launcher keeps the assertion pinned to what is really sent to Nextflow.
    """
    defaults = _param_checker().extract_config_defaults(
        (REPO_ROOT / "nextflow.config").read_text())
    script = (BENCH / "run_arms.sh").read_text()
    flags = set(re.findall(r"^\s*add_param\s+(\w+)", script, re.M))
    assert flags, "no add_param calls found — did run_arms.sh change shape?"
    unknown = sorted(flags - set(defaults))
    assert not unknown, (
        f"run_arms.sh passes --flags absent from nextflow.config: {unknown}")


def test_every_flag_run_arms_passes_is_carried_by_the_plan(plan):
    """A flag the launcher reads but the plan never emits is always blank, so the
    arm silently runs at the pipeline default instead of the configuration named."""
    script = (BENCH / "run_arms.sh").read_text()
    flags = set(re.findall(r"^\s*add_param\s+(\w+)", script, re.M))
    columns = {k for r in plan for k in r}
    missing = sorted(flags - columns)
    assert not missing, (
        f"run_arms.sh reads plan columns that build_arm_plan never writes: {missing}")


def test_registration_arms_all_pin_reg_qc_2(plan):
    """reg_qc=2 is what emits the staged QC the whole arm ranking reads.

    An arm at reg_qc<2 does not fail — it produces zero accuracy rows, and the
    consumer renders an empty page with no error.
    """
    reg = [r for r in plan if r["arm_kind"] == "registration"]
    assert reg, "no registration arms in the plan"
    assert all(r["reg_qc"] == 2 for r in reg)


def test_baseline_reg_qc_must_be_2(cfg):
    bad = dict(cfg, baseline=dict(cfg["baseline"], reg_qc=1))
    with pytest.raises(ValueError, match="reg_qc must be 2"):
        build_arm_plan(bad)


# ---------------------------------------------------------------------------
# The backend contract — VALIS-only params must not appear on a tiled arm
# ---------------------------------------------------------------------------

def test_tiled_arm_carries_no_valis_only_params(plan):
    """memory_mode / reg_micro_reg do not exist on the STARE backend.

    Writing a value there would (a) pass `--memory_mode X` to a run that ignores
    it and (b) tell the consumer the tiled arm sat in a cell of the preset x
    depth grid, which is exactly the "one point of comparison, not a seventh
    cell" distinction registration_arms.R is built around.
    """
    tiled = [r for r in plan if r.get("backend") == "tiled"]
    assert tiled, "the tiled comparator arm is missing from the plan"
    for r in tiled:
        assert r["memory_mode"] == ""
        assert r["reg_micro_reg"] == ""


def test_valis_arms_are_preset_x_depth_not_a_2x2(plan):
    """reg_micro_reg is a DEPTH (0/1/2), so 2 presets x 3 depths = 6 arms."""
    valis = [r for r in plan
             if r["arm_kind"] == "registration" and r.get("backend") == "valis"
             and "_seg" not in r["arm"]]
    assert len(valis) == 6, [r["arm"] for r in valis]
    assert {r["reg_micro_reg"] for r in valis} == {0, 1, 2}


# ---------------------------------------------------------------------------
# The QC-segmenter cross
# ---------------------------------------------------------------------------

def test_qc_segmenter_cross_varies_seg_method_only(cfg, plan):
    """A cross arm must differ from its base arm in seg_method and NOTHING else.

    The claim the axis makes is "identical registration, different measuring
    instrument". If a cross arm also moved memory_mode, the difference would be
    a registration difference wearing a robustness label.
    """
    ref = cfg["qc_segmenter_cross"]["reference_arm"]
    base = next(r for r in plan if r["arm"] == ref)
    crossed = [r for r in plan if r["arm"].startswith(f"{ref}_seg")]
    assert crossed, "qc_segmenter_cross produced no arms"
    for r in crossed:
        assert r["seg_method"] != base["seg_method"]
        for k in ("memory_mode", "reg_micro_reg", "registration_method", "reg_qc"):
            assert r[k] == base[k], f"{r['arm']} moved {k} as well as seg_method"


def test_cross_never_duplicates_the_base_arm(cfg, plan):
    """The baseline segmenter is already the base arm; re-running it measures nothing."""
    names = [r["arm"] for r in plan]
    assert len(names) == len(set(names)), "duplicate arm names in the plan"
    default = cfg["baseline"]["seg_method"]
    assert not any(r["arm"].endswith(f"_seg{default}") for r in plan)


def test_reference_arm_must_name_a_real_arm(cfg):
    bad = dict(cfg, qc_segmenter_cross=dict(cfg["qc_segmenter_cross"],
                                            reference_arm="valis_medium_micro9"))
    with pytest.raises(ValueError, match="is not an arm this config produces"):
        build_arm_plan(bad)


def test_cross_all_crosses_every_arm(cfg):
    full = build_arm_plan(dict(cfg, qc_segmenter_cross=dict(
        cfg["qc_segmenter_cross"], cross="all")))
    reg = [r for r in full if r["arm_kind"] == "registration"]
    n_methods = len(cfg["qc_segmenter_cross"]["seg_method"])
    assert len(reg) == 7 * n_methods, (
        "cross: all should be 7 arms x every segmenter; the README quotes 21 "
        "and the cost gate in arms.yaml depends on that number being right")


# ---------------------------------------------------------------------------
# Arm factoring — the segmentation arms must RESUME, not re-register
# ---------------------------------------------------------------------------

def test_segmentation_arms_resume_from_a_registration_arm(cfg, plan):
    seg = [r for r in plan if r["arm_kind"] == "segmentation"]
    assert seg
    reg_names = {r["arm"] for r in plan if r["arm_kind"] == "registration"}
    for r in seg:
        assert r["start"] == "segmentation", (
            "a segmentation arm that does not --start segmentation re-runs "
            "registration, which is the cost this factoring exists to avoid")
        assert r["from_arm"] in reg_names


def test_from_arm_must_name_a_real_arm(cfg):
    bad = dict(cfg, segmentation_arms=dict(cfg["segmentation_arms"],
                                           from_arm="valis_nope"))
    with pytest.raises(ValueError, match="names no registration arm"):
        build_arm_plan(bad)


def test_registration_arms_stop_at_registration(plan):
    """Nothing downstream of registration changes the staged registration QC."""
    for r in plan:
        if r["arm_kind"] == "registration":
            assert (r["start"], r["stop"]) == ("preprocessing", "registration")


def test_compute_arm_runs_the_whole_pipeline(plan):
    """The compute profile is the ONLY arm that measures the skipped processes."""
    comp = [r for r in plan if r["arm_kind"] == "compute"]
    assert comp
    for r in comp:
        assert r["start"] == "" and r["stop"] == "", (
            "a gated compute arm cannot price SEGMENT / quantification / export, "
            "which is the only reason this arm exists")


# ---------------------------------------------------------------------------
# The CONSUMER contract (ihc_method/code/registration_arms.R)
# ---------------------------------------------------------------------------

def test_arms_manifest_has_exactly_the_columns_the_consumer_reads(plan):
    """registration_arms.R keeps intersect(c(arm_dir, backend, memory_mode,
    micro_reg, label), names(man)). A renamed column is not an error there — it
    is silently dropped, and the arm falls back to directory-name parsing."""
    rows = arms_manifest_rows(plan)
    assert rows
    assert set(rows[0]) == {"arm_dir", "backend", "memory_mode", "micro_reg", "label"}


def test_arms_manifest_lists_registration_arms_only(plan):
    """A segmentation or compute arm in arms.csv becomes an unlabelled box in
    every panel of a page that ranks REGISTRATION configurations."""
    dirs = {r["arm_dir"] for r in arms_manifest_rows(plan)}
    assert not any(d.startswith(("seg_", "compute_")) for d in dirs)


def test_every_manifest_arm_has_a_label(plan):
    """The label bypasses directory-name parsing. Without it a mislabelled arm
    renders a clean figure with the conclusion inverted."""
    for r in arms_manifest_rows(plan):
        assert r["label"], r


def test_tiled_arm_name_survives_the_consumer_fallback(plan):
    """If arms.csv is lost, registration_arms.R reads a directory containing
    `tiled` or `stare` as the tiled backend. The name must satisfy that too."""
    tiled = [r["arm_dir"] for r in arms_manifest_rows(plan) if r["backend"] == "tiled"]
    assert tiled
    for d in tiled:
        assert "tiled" in d or "stare" in d


# ---------------------------------------------------------------------------
# The BASH contract — run_arms.sh parses arm_plan.csv with a naive IFS split
# ---------------------------------------------------------------------------

def _plan_csv(plan) -> str:
    fields: list[str] = []
    for r in plan:
        for k in r:
            if k not in fields:
                fields.append(k)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n", restval="")
    w.writeheader()
    w.writerows(plan)
    return buf.getvalue()


def test_no_plan_value_contains_a_comma(plan):
    """run_arms.sh splits on IFS=',' and does NOT honour CSV quoting.

    A comma in any value shifts every later column on that row, so the run
    launches a configuration nobody chose — and it still exits 0. This is why
    the human-readable label lives in arms.csv (read by R's readr, which does
    honour quoting) and never in the plan.
    """
    for r in plan:
        for k, v in r.items():
            assert "," not in str(v), f"{k}={v!r} in arm {r['arm']} would break run_arms.sh"


def test_plan_csv_is_written_unquoted(plan):
    assert '"' not in _plan_csv(plan)


def test_every_plan_row_has_every_column(plan):
    """A short row shifts the columns after it in run_arms.sh exactly as an
    embedded comma does. DictWriter pads with restval='' — this asserts the
    padding is actually reached rather than assumed."""
    text = _plan_csv(plan)
    n = len(text.splitlines()[0].split(","))
    for line in text.splitlines()[1:]:
        assert len(line.split(",")) == n, line


# ---------------------------------------------------------------------------
# Samplesheet reading
# ---------------------------------------------------------------------------

def test_read_input_patients_dedupes_in_first_seen_order(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text(
        "patient_id,path_to_file,is_reference,channels\n"
        "B,/b1.tif,true,DAPI\n"
        "B,/b2.tif,false,DAPI\n"
        "A,/a1.tif,true,DAPI\n")
    assert read_input_patients(p) == ["B", "A"]


def test_read_input_patients_rejects_a_sheet_with_no_patient_id(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("sample,path\nX,/x.tif\n")
    with pytest.raises(ValueError, match="patient_id"):
        read_input_patients(p)


# ---------------------------------------------------------------------------
# The shell scripts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script", ["run_arms.sh", "pull_to_ihc_method.sh"])
def test_shell_scripts_parse(script):
    """`bash -n` on both. A ${1:?...} error word containing an apostrophe once
    swallowed the rest of pull_to_ihc_method.sh and reported the failure ~30
    lines later, so this is checked rather than eyeballed."""
    r = subprocess.run(["bash", "-n", str(BENCH / script)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("script", ["run_arms.sh", "pull_to_ihc_method.sh"])
def test_shell_scripts_are_tracked_executable(script):
    """Not cosmetic: a non-executable script fails at launch with exit 126, and
    a local chmod does not reach the cluster's git checkout."""
    r = subprocess.run(["git", "ls-files", "-s", f"benchmarks/{script}"],
                       cwd=REPO_ROOT, capture_output=True, text=True)
    assert r.stdout.startswith("100755"), r.stdout or "file not tracked"
