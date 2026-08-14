"""EXPORT_SPATIALDATA's registration QC must be selected per patient, never run-wide.

WHY THIS EXISTS -- the bug it was written against
-------------------------------------------------
`EXPORT_SPATIALDATA` is a PER-PATIENT process: one task, one `<patient>.zarr`.
Its reg_qc=2 inputs used to arrive on two separate `path` channels fed by
`.collect(sort: true)` over the whole run, so a Nextflow value channel carrying
EVERY patient's `*_seg_qc.json` and `*_reg_residuals.csv` was broadcast into
EVERY patient's task.

That is not untidy; it is wrong data. `bin/export_spatialdata.py` joins residual
rows onto this patient's cell centroids by raw reference-frame pixel coordinate,
and every patient's reference frame is its own pixel grid rooted at (0, 0), so a
foreign patient's rows land on whatever cell happens to sit within
`params.spatialdata_residual_join_max_px` and are written into the store as an
`obsm` column named after a slide this patient never had -- a column whose whole
purpose is to EXCLUDE cells. `load_qc` likewise keyed on the slide name alone, so
one patient's `uns["qc"]["registration"]` carried the whole run's.

Observed on a two-patient stub run before the fix: BOTH `EXPORT_SPATIALDATA`
task directories staged all four QC artifacts --
`P001_..._reg_residuals.csv`, `P001_..._seg_qc.json`,
`P002_..._reg_residuals.csv`, `P002_..._seg_qc.json` -- with P001's residuals
joining onto P002's cells at 100% of pairs, and vice versa. The suite could not
see it: `tests/testdata/test_input.csv` is one patient, and with one patient
"every QC file in the run" and "this patient's QC files" are the same set.
`tests/testdata/test_input_two_patients.csv` now exists so they are not.

THE RULE
--------
The QC artifacts must reach the process on the SAME per-patient tuple as every
other input, joined by `patient_id`. That is enforced structurally rather than by
grepping for `collect(`: a per-patient process with exactly ONE input channel has
nowhere to put a run-wide broadcast, because a broadcast needs an input of its
own. Both halves are checked --

  1. `modules/local/export_spatialdata.nf` declares exactly one input, a tuple,
     and `qc_json`/`reg_residuals` are inside it;
  2. `subworkflows/local/postprocess.nf` calls `EXPORT_SPATIALDATA(...)` with
     exactly one argument.

Check 1 alone would pass a call site that re-broadcast via `.combine(collected)`;
check 2 alone would pass a module that took the run-wide list back as a second
input. Together they leave no shape in which a run-wide list reaches a
per-patient task without this test failing.

This is a static check because the behavioural counterpart is an nf-test, and
nf-test's blocking CI gate runs in `-stub` mode where the leak is visible only in
the staged work directory, not in any published artifact the suite compares.
"""

from __future__ import annotations

from pathlib import Path

from _code_view import code_view as _code_view

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE = REPO_ROOT / "modules" / "local" / "export_spatialdata.nf"
SUBWORKFLOW = REPO_ROOT / "subworkflows" / "local" / "postprocess.nf"


def _input_block(code: str) -> str:
    """The text between the process's `input:` and the following `output:` label."""
    start = code.index("input:") + len("input:")
    end = code.index("output:", start)
    return code[start:end]


def _declared_inputs(code: str) -> list[str]:
    """One entry per declared process input.

    An input declaration starts at the beginning of a line with `val`, `path`,
    `tuple`, `env`, `stdin` or `each`; a declaration may wrap across lines, so
    lines are joined onto the most recent starter.
    """
    starters = ("val ", "val(", "path ", "path(", "tuple ", "env ", "stdin", "each ")
    decls: list[str] = []
    for raw in _input_block(code).splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(starters):
            decls.append(line)
        elif decls:
            decls[-1] += " " + line
    return decls


def _call_args(code: str, name: str) -> list[str]:
    """Top-level, comma-separated arguments of the single `name(...)` call."""
    i = code.index(f"\n        {name}(")
    open_paren = code.index("(", i)
    depth, args, buf = 0, [], ""
    for ch in code[open_paren:]:
        if ch in "([{":
            depth += 1
            if depth == 1:
                continue
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                break
        if depth == 1 and ch == ",":
            args.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        args.append(buf)
    return [a.strip() for a in args if a.strip()]


def test_export_spatialdata_declares_one_per_patient_input():
    """A per-patient process with one input has nowhere to put a run-wide list."""
    decls = _declared_inputs(_code_view(MODULE))
    assert len(decls) == 1, (
        "EXPORT_SPATIALDATA is per-patient (one task, one <patient>.zarr). A second "
        "input channel is how a run-wide `collect()` of every patient's QC gets "
        f"broadcast into every patient's task. Declared inputs: {decls}"
    )
    only = decls[0]
    assert only.startswith("tuple "), only
    for name in ("qc_json", "reg_residuals"):
        assert f"path({name})" in only, (
            f"{name} must ride the per-patient tuple, joined by patient_id like every "
            f"other input to this process. Input declaration: {only}"
        )


def test_postprocess_calls_export_spatialdata_with_one_channel():
    """The call site must not hand it a second, run-wide channel."""
    args = _call_args(_code_view(SUBWORKFLOW), "EXPORT_SPATIALDATA")
    assert len(args) == 1, (
        "EXPORT_SPATIALDATA must be called with exactly one per-patient channel. "
        f"Extra arguments are run-wide broadcasts. Got {len(args)}: {args}"
    )
