> **SUPERSEDED — historical record, 2026-09-02 (MIRAGE v1.0.0, release plan 13).**
> The ANHIR/ACROBAT landmark harness this document plans (`benchmarks/registration_eval/`)
> and the synthetic ground-truth rung that replaced it (`benchmarks/stare_bench/`) were
> both deleted and exist on no branch. `benchmarks/README.md` section B is the removal
> record. Nothing below is runnable; it is kept because it is the dated record of a
> decision that was actually taken. Do not edit the body — a June plan rewritten to
> describe a September tree is a lie about its own date.

# Benchmarking Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the benchmark foundation — a dedicated `enable_size_logs` flag, the relocated size-log aggregation, a `-profile benchmark`, the single-image→(size×channel) matrix generator, and the parameter-sweep run-plan builder — establishing the data contracts every downstream component consumes.

**Architecture:** All new code lives under `benchmarks/`. Nextflow edits are additive/gated: a new param swaps the existing trace-gated aggregation onto its own flag, and `aggregate_size_logs.nf` relocates into `benchmarks/modules/`. The matrix generator and run-plan builder are pure-Python (pyvips/tifffile/numpy + PyYAML) with unit-tested core functions, so heavy WSI I/O is isolated from the testable logic.

**Tech Stack:** Nextflow DSL2, Python 3 (numpy, tifffile, pyvips, PyYAML, pandas), pytest.

This is **Plan 1 of 4**. Subsequent plans: (2) registration-accuracy evaluator, (3) analysis notebook + optimal-config generator, (4) docs. Plan 1 fixes the `matrix_manifest.csv` and `run_plan.csv` schemas that plans 2–3 read.

**Spec:** `docs/superpowers/specs/2026-06-16-benchmarking-framework-design.md`

---

## File Structure

- Create: `benchmarks/configs/benchmark.config` — `benchmark` profile (enables trace + size logs, per-run trace dir).
- Create: `benchmarks/configs/sweep.yaml` — sweep axes (image size/channels + pipeline params), strategy.
- Create: `benchmarks/generate_matrix.py` — source image → matrix OME-TIFFs + `matrix_manifest.csv`.
- Create: `benchmarks/build_run_plan.py` — `sweep.yaml` + manifest → `run_plan.csv` (testable enumeration).
- Create: `benchmarks/run_sweep.sh` — thin driver: loop `run_plan.csv` → launch pipeline per run.
- Create: `benchmarks/tests/test_generate_matrix.py`, `benchmarks/tests/test_build_run_plan.py`.
- Create: `benchmarks/README.md`.
- Move: `modules/local/aggregate_size_logs.nf` → `benchmarks/modules/aggregate_size_logs.nf`.
- Modify: `nextflow.config:182-185` (add `enable_size_logs`), `nextflow_schema.json:517-526` (schema), `workflows/mirage.nf:10` (include path) + `:200` (gate flag).

---

## Task 1: Add the `enable_size_logs` flag and gate aggregation on it

**Files:**
- Modify: `nextflow.config:182-185`
- Modify: `nextflow_schema.json:516-521`
- Modify: `workflows/mirage.nf:200`

- [ ] **Step 1: Add the param in `nextflow.config`**

Replace lines 182-185:

```groovy
    // Tracing (optional)
    enable_trace     = true             // Enable detailed execution tracing
    trace_dir        = '.trace'          // Directory for trace outputs (independent of outdir)

```

with:

```groovy
    // Tracing (optional)
    enable_trace     = true             // Enable detailed execution tracing
    trace_dir        = '.trace'          // Directory for trace outputs (independent of outdir)
    enable_size_logs = false            // Aggregate per-process input-size logs (benchmark layer; off in production)

```

- [ ] **Step 2: Add the param to `nextflow_schema.json`**

After the `debug_channels` block (line 516, the closing `},` before `"enable_trace"`), insert:

```json
                "enable_size_logs": {
                    "type": "boolean",
                    "description": "Aggregate per-process input-size logs for benchmarking. Off in production; enabled by -profile benchmark.",
                    "default": false
                },
```

- [ ] **Step 3: Gate the aggregation on the new flag**

In `workflows/mirage.nf:200`, change:

```groovy
    if (params.enable_trace) {
```

to:

```groovy
    if (params.enable_size_logs) {
```

(Leave the `enable_trace`-gated `trace {}` / `report {}` / `timeline {}` blocks in `nextflow.config:305-322` untouched — Nextflow's trace and the size-log aggregation are now independently switchable.)

- [ ] **Step 4: Verify the stub pipeline still launches with the flag off (default)**

Run: `nextflow run . -profile test,docker -stub --outdir results_smoke 2>&1 | tail -20`
Expected: completes; no `size_logs/` directory under `results_smoke`.

- [ ] **Step 5: Verify aggregation runs when the flag is on**

Run: `nextflow run . -profile test,docker -stub --enable_size_logs true --outdir results_smoke2 2>&1 | tail -20`
Expected: completes; `AGGREGATE_SIZE_LOGS` appears in the process list.

- [ ] **Step 6: Commit**

```bash
git add nextflow.config nextflow_schema.json workflows/mirage.nf
git commit -m ":sparkles: add enable_size_logs flag gating size-log aggregation

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Relocate `aggregate_size_logs.nf` into the benchmark layer

**Files:**
- Move: `modules/local/aggregate_size_logs.nf` → `benchmarks/modules/aggregate_size_logs.nf`
- Modify: `workflows/mirage.nf:10`

- [ ] **Step 1: Move the module file with git**

```bash
mkdir -p benchmarks/modules
git mv modules/local/aggregate_size_logs.nf benchmarks/modules/aggregate_size_logs.nf
```

- [ ] **Step 2: Update the include path in `workflows/mirage.nf:10`**

Change:

```groovy
include { AGGREGATE_SIZE_LOGS         } from '../modules/local/aggregate_size_logs'
```

to:

```groovy
include { AGGREGATE_SIZE_LOGS         } from '../../benchmarks/modules/aggregate_size_logs'
```

- [ ] **Step 3: Verify the include resolves (stub run with flag on)**

Run: `nextflow run . -profile test,docker -stub --enable_size_logs true --outdir results_move 2>&1 | tail -20`
Expected: completes; no "Cannot find module" / include error; `AGGREGATE_SIZE_LOGS` runs.

- [ ] **Step 4: Commit**

```bash
git add -A modules/local benchmarks/modules workflows/mirage.nf
git commit -m ":truck: move aggregate_size_logs into benchmarks layer

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Add the `benchmark` profile

**Files:**
- Create: `benchmarks/configs/benchmark.config`

- [ ] **Step 1: Write the profile config**

Create `benchmarks/configs/benchmark.config`:

```groovy
/*
 * benchmark profile — enable full tracing + size-log aggregation.
 *
 * Usage (one run per sweep cell; run_sweep.sh sets --trace_dir / --outdir per run):
 *   nextflow run . -profile docker -c benchmarks/configs/benchmark.config \
 *     --input <samplesheet> --outdir <run_outdir> --trace_dir <run_trace>
 */
params {
    enable_trace     = true
    enable_size_logs = true
}
```

- [ ] **Step 2: Verify the config layers cleanly on a stub run**

Run: `nextflow run . -profile test,docker -stub -c benchmarks/configs/benchmark.config --outdir results_prof 2>&1 | tail -20`
Expected: completes; `AGGREGATE_SIZE_LOGS` runs (size logs enabled by the config).

- [ ] **Step 3: Commit**

```bash
git add benchmarks/configs/benchmark.config
git commit -m ":wrench: add benchmark profile config

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Matrix generator — core pure functions (TDD)

**Files:**
- Create: `benchmarks/generate_matrix.py`
- Test: `benchmarks/tests/test_generate_matrix.py`

- [ ] **Step 1: Write the failing tests for the pure functions**

Create `benchmarks/tests/test_generate_matrix.py`:

```python
import numpy as np
import pytest
from benchmarks.generate_matrix import compute_target_shape, synthesize_channels


def test_compute_target_shape_scales_long_edge_preserving_aspect():
    # source 4000x2000 (HxW), target long edge 1000 -> 1000x500
    assert compute_target_shape((4000, 2000), 1000) == (1000, 500)


def test_compute_target_shape_long_edge_is_height_when_taller():
    assert compute_target_shape((2000, 4000), 1000) == (500, 1000)


def test_synthesize_channels_returns_requested_count_and_shape():
    src = np.full((8, 8), 100, dtype=np.uint8)
    out = synthesize_channels(src, n_channels=4, seed=0)
    assert out.shape == (4, 8, 8)
    assert out.dtype == np.uint8


def test_synthesize_channels_first_channel_is_source_unchanged():
    src = np.arange(16, dtype=np.uint8).reshape(4, 4)
    out = synthesize_channels(src, n_channels=3, seed=0)
    np.testing.assert_array_equal(out[0], src)


def test_synthesize_channels_extra_channels_differ_from_source():
    src = np.full((16, 16), 120, dtype=np.uint8)
    out = synthesize_channels(src, n_channels=2, seed=0)
    assert not np.array_equal(out[1], src)  # jitter+noise+offset perturbs it


def test_synthesize_channels_is_deterministic_for_seed():
    src = np.full((16, 16), 120, dtype=np.uint8)
    a = synthesize_channels(src, n_channels=3, seed=7)
    b = synthesize_channels(src, n_channels=3, seed=7)
    np.testing.assert_array_equal(a, b)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest benchmarks/tests/test_generate_matrix.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'benchmarks.generate_matrix'` (or import error).

- [ ] **Step 3: Implement the pure functions**

Create `benchmarks/generate_matrix.py`:

```python
"""Generate a (size x channels) benchmark matrix from a single source image.

Pure functions (compute_target_shape, synthesize_channels) are unit-tested.
Heavy I/O (read/resize/write OME-TIFF) is isolated in run_matrix().
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np


def compute_target_shape(src_hw: tuple[int, int], target_long_edge: int) -> tuple[int, int]:
    """Scale (height, width) so the longer edge equals target_long_edge, preserving aspect."""
    h, w = src_hw
    long_edge = max(h, w)
    scale = target_long_edge / float(long_edge)
    return (round(h * scale), round(w * scale))


def synthesize_channels(src_2d: np.ndarray, n_channels: int, seed: int = 0) -> np.ndarray:
    """Replicate a single 2-D channel into n_channels with per-channel perturbation.

    Channel 0 is the unmodified source. Channels 1..N-1 add intensity jitter,
    Gaussian noise, and a 1-px roll offset so each channel is non-identical.
    """
    if src_2d.ndim != 2:
        raise ValueError("src_2d must be 2-D (H, W)")
    rng = np.random.default_rng(seed)
    info = np.iinfo(src_2d.dtype)
    out = np.empty((n_channels,) + src_2d.shape, dtype=src_2d.dtype)
    out[0] = src_2d
    for c in range(1, n_channels):
        gain = 1.0 + rng.uniform(-0.1, 0.1)
        noise = rng.normal(0.0, 3.0, size=src_2d.shape)
        shifted = np.roll(src_2d, shift=c, axis=1)
        vals = np.clip(shifted.astype(np.float64) * gain + noise, info.min, info.max)
        out[c] = vals.astype(src_2d.dtype)
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest benchmarks/tests/test_generate_matrix.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add benchmarks/generate_matrix.py benchmarks/tests/test_generate_matrix.py
git commit -m ":sparkles: matrix generator core functions (size scaling, channel synthesis)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Matrix generator — I/O wrapper, manifest, and CLI

**Files:**
- Modify: `benchmarks/generate_matrix.py`
- Test: `benchmarks/tests/test_generate_matrix.py`

- [ ] **Step 1: Write the failing test for manifest + OME-TIFF output**

Append to `benchmarks/tests/test_generate_matrix.py`:

```python
def test_run_matrix_writes_cells_and_manifest(tmp_path):
    import tifffile
    from benchmarks.generate_matrix import run_matrix

    src = tmp_path / "src.tif"
    tifffile.imwrite(src, np.full((400, 200), 100, dtype=np.uint8))

    manifest = run_matrix(
        source=src, outdir=tmp_path / "matrix",
        target_px=[100, 50], n_channels=[1, 2], seed=0,
    )

    rows = list(csv.DictReader(open(manifest)))
    # 2 sizes x 2 channel-counts = 4 cells
    assert len(rows) == 4
    assert set(rows[0].keys()) == {
        "cell_id", "target_px", "width", "height", "n_channels", "bytes", "path",
    }
    for r in rows:
        p = Path(r["path"])
        assert p.exists() and int(r["bytes"]) == p.stat().st_size
        arr = tifffile.imread(p)
        n = int(r["n_channels"])
        # single-channel cells are 2-D; multi-channel are (C, H, W)
        assert (arr.ndim == 2 and n == 1) or (arr.shape[0] == n)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest benchmarks/tests/test_generate_matrix.py::test_run_matrix_writes_cells_and_manifest -v`
Expected: FAIL — `ImportError: cannot import name 'run_matrix'`.

- [ ] **Step 3: Implement `run_matrix`, resize helper, and CLI**

Append to `benchmarks/generate_matrix.py`:

```python
def _resize(src_2d: np.ndarray, target_hw: tuple[int, int]) -> np.ndarray:
    """Resize a 2-D array to target (H, W). Uses pyvips if available, else PIL."""
    th, tw = target_hw
    try:
        import pyvips
        vi = pyvips.Image.new_from_memory(
            src_2d.tobytes(), src_2d.shape[1], src_2d.shape[0], 1, "uchar"
        )
        vi = vi.resize(tw / src_2d.shape[1], vscale=th / src_2d.shape[0])
        buf = vi.write_to_memory()
        return np.frombuffer(buf, dtype=np.uint8).reshape(th, tw)
    except ModuleNotFoundError:
        from PIL import Image
        im = Image.fromarray(src_2d).resize((tw, th), Image.BILINEAR)
        return np.asarray(im, dtype=src_2d.dtype)


def _read_source_2d(path: Path) -> np.ndarray:
    import tifffile
    arr = tifffile.imread(path)
    if arr.ndim == 3:  # collapse to a single representative channel
        arr = arr[0] if arr.shape[0] <= arr.shape[-1] else arr[..., 0]
    return arr


def run_matrix(source, outdir, target_px, n_channels, seed=0):
    import tifffile

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    src = _read_source_2d(Path(source))
    manifest_path = outdir / "matrix_manifest.csv"

    with open(manifest_path, "w", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["cell_id", "target_px", "width", "height", "n_channels", "bytes", "path"],
        )
        writer.writeheader()
        for tpx in target_px:
            th, tw = compute_target_shape(src.shape, tpx)
            resized = _resize(src, (th, tw))
            for nch in n_channels:
                cell_id = f"px{tpx}_ch{nch}"
                out_path = outdir / f"{cell_id}.ome.tif"
                stack = synthesize_channels(resized, nch, seed=seed)
                data = stack[0] if nch == 1 else stack
                channel_names = [f"ch{i}" for i in range(nch)]
                tifffile.imwrite(
                    out_path,
                    data,
                    photometric="minisblack",
                    metadata={"axes": "YX" if nch == 1 else "CYX",
                              "Channel": {"Name": channel_names}},
                )
                writer.writerow({
                    "cell_id": cell_id, "target_px": tpx, "width": tw, "height": th,
                    "n_channels": nch, "bytes": out_path.stat().st_size, "path": str(out_path),
                })
    return manifest_path


def main():
    ap = argparse.ArgumentParser(description="Generate a (size x channels) benchmark matrix.")
    ap.add_argument("--source", required=True, type=Path, help="Source image (user-supplied).")
    ap.add_argument("--outdir", required=True, type=Path)
    ap.add_argument("--target-px", type=int, nargs="+", default=[2048, 4096, 8192, 16384, 32768])
    ap.add_argument("--n-channels", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    path = run_matrix(a.source, a.outdir, a.target_px, a.n_channels, a.seed)
    print(f"Wrote matrix manifest: {path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the full generator test module**

Run: `pytest benchmarks/tests/test_generate_matrix.py -v`
Expected: PASS (7 passed). If pyvips is absent the PIL fallback is used (add `pillow` to the test env if needed).

- [ ] **Step 5: Commit**

```bash
git add benchmarks/generate_matrix.py benchmarks/tests/test_generate_matrix.py
git commit -m ":sparkles: matrix generator I/O, OME-TIFF output, and manifest

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Sweep config + run-plan builder (TDD)

**Files:**
- Create: `benchmarks/configs/sweep.yaml`
- Create: `benchmarks/build_run_plan.py`
- Test: `benchmarks/tests/test_build_run_plan.py`

- [ ] **Step 1: Write the sweep config**

Create `benchmarks/configs/sweep.yaml`:

```yaml
# Benchmark sweep definition. strategy: ofat (one-factor-at-a-time) | grid.
strategy: ofat
# Baseline used as the fixed point for ofat (one value per axis).
baseline:
  target_px: 4096
  n_channels: 2
  memory_mode: medium
  reg_use_tiled_registration: true
  reg_tile_size: 2048
  reg_n_workers: 4
  skip_micro_registration: true
  reg_distributed_tiling: false
# Axes to vary. Each maps to a --<name> CLI override (target_px/n_channels drive the image matrix).
axes:
  target_px:                  [2048, 4096, 8192, 16384, 32768]
  n_channels:                 [1, 2, 4, 8]
  memory_mode:                [low, medium, high]
  reg_use_tiled_registration: [true, false]
  reg_tile_size:              [1024, 2048, 4096]
  reg_n_workers:              [2, 4, 8]
  skip_micro_registration:    [true, false]
  reg_distributed_tiling:     [false, true]
```

- [ ] **Step 2: Write the failing tests for the run-plan builder**

Create `benchmarks/tests/test_build_run_plan.py`:

```python
from benchmarks.build_run_plan import build_run_plan

SWEEP = {
    "strategy": "ofat",
    "baseline": {"target_px": 4096, "n_channels": 2, "memory_mode": "medium"},
    "axes": {
        "target_px": [4096, 8192],
        "n_channels": [2, 4],
        "memory_mode": ["medium", "high"],
    },
}


def test_ofat_includes_one_baseline_run():
    plan = build_run_plan(SWEEP)
    baseline_runs = [r for r in plan if r["varied_axis"] == "baseline"]
    assert len(baseline_runs) == 1
    assert baseline_runs[0]["memory_mode"] == "medium"


def test_ofat_varies_one_axis_at_a_time_off_baseline():
    plan = build_run_plan(SWEEP)
    # non-baseline values: target_px=8192, n_channels=4, memory_mode=high -> 3 runs
    varied = [r for r in plan if r["varied_axis"] != "baseline"]
    assert len(varied) == 3
    hi = [r for r in varied if r["varied_axis"] == "memory_mode"][0]
    # only memory_mode differs from baseline; other axes stay at baseline values
    assert hi["memory_mode"] == "high" and hi["target_px"] == 4096 and hi["n_channels"] == 2


def test_run_ids_are_unique():
    plan = build_run_plan(SWEEP)
    ids = [r["run_id"] for r in plan]
    assert len(ids) == len(set(ids))


def test_grid_is_full_cross_product():
    sweep = dict(SWEEP, strategy="grid")
    plan = build_run_plan(sweep)
    # 2 * 2 * 2 = 8 cells
    assert len(plan) == 8
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest benchmarks/tests/test_build_run_plan.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'benchmarks.build_run_plan'`.

- [ ] **Step 4: Implement the run-plan builder**

Create `benchmarks/build_run_plan.py`:

```python
"""Expand a sweep.yaml into a flat run plan (one row per pipeline launch)."""
from __future__ import annotations

import argparse
import csv
import itertools
from pathlib import Path


def build_run_plan(sweep: dict) -> list[dict]:
    strategy = sweep.get("strategy", "ofat")
    axes = sweep["axes"]
    if strategy == "grid":
        keys = list(axes)
        rows = []
        for i, combo in enumerate(itertools.product(*(axes[k] for k in keys))):
            row = dict(zip(keys, combo))
            row["run_id"] = f"run{i:04d}"
            row["varied_axis"] = "grid"
            rows.append(row)
        return rows

    # ofat: one baseline run, then one run per non-baseline value of each axis
    baseline = sweep["baseline"]
    rows = [dict(baseline, run_id="run0000", varied_axis="baseline")]
    i = 1
    for axis, values in axes.items():
        for v in values:
            if v == baseline.get(axis):
                continue
            row = dict(baseline)
            row[axis] = v
            row["run_id"] = f"run{i:04d}"
            row["varied_axis"] = axis
            rows.append(row)
            i += 1
    return rows


def main():
    import yaml

    ap = argparse.ArgumentParser(description="Expand sweep.yaml into run_plan.csv")
    ap.add_argument("--sweep", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    sweep = yaml.safe_load(a.sweep.read_text())
    plan = build_run_plan(sweep)
    fields = ["run_id", "varied_axis"] + [k for k in plan[0] if k not in ("run_id", "varied_axis")]
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(plan)
    print(f"Wrote {len(plan)} runs to {a.out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest benchmarks/tests/test_build_run_plan.py -v`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add benchmarks/configs/sweep.yaml benchmarks/build_run_plan.py benchmarks/tests/test_build_run_plan.py
git commit -m ":sparkles: sweep config and run-plan builder (ofat + grid)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Sweep driver + README

**Files:**
- Create: `benchmarks/run_sweep.sh`
- Create: `benchmarks/README.md`

- [ ] **Step 1: Write the driver script**

Create `benchmarks/run_sweep.sh`:

```bash
#!/usr/bin/env bash
# Drive a benchmark sweep: one pipeline launch per run_plan.csv row.
# Usage: benchmarks/run_sweep.sh <run_plan.csv> <matrix_manifest.csv> <results_root> [extra nextflow args...]
set -euo pipefail

RUN_PLAN="${1:?run_plan.csv}"
MANIFEST="${2:?matrix_manifest.csv}"
ROOT="${3:?results root}"
shift 3
EXTRA=("$@")

mkdir -p "$ROOT"
header=$(head -n1 "$RUN_PLAN")
IFS=',' read -r -a cols <<< "$header"

tail -n +2 "$RUN_PLAN" | while IFS=',' read -r -a vals; do
  declare -A row=()
  for i in "${!cols[@]}"; do row["${cols[$i]}"]="${vals[$i]}"; done
  run_id="${row[run_id]}"
  cell_id="px${row[target_px]}_ch${row[n_channels]}"

  # Resolve this cell's image from the matrix manifest -> build a 1-row samplesheet.
  img=$(awk -F',' -v c="$cell_id" 'NR>1 && $1==c {print $7}' "$MANIFEST")
  if [[ -z "$img" ]]; then echo "WARN: no matrix cell $cell_id; skipping $run_id" >&2; continue; fi

  run_dir="$ROOT/$run_id"; mkdir -p "$run_dir"
  sheet="$run_dir/samplesheet.csv"
  printf 'patient_id,image,channels,is_reference\n%s,%s,ch0,true\n' "$cell_id" "$img" > "$sheet"

  # Map non-structural columns (skip run_id/varied_axis/target_px/n_channels) to --<param>.
  params=()
  for k in "${cols[@]}"; do
    case "$k" in run_id|varied_axis|target_px|n_channels) continue;; esac
    params+=("--${k}" "${row[$k]}")
  done

  echo ">>> $run_id (varied=${row[varied_axis]}, cell=$cell_id)"
  nextflow run . -profile docker \
    -c benchmarks/configs/benchmark.config \
    --input "$sheet" --outdir "$run_dir/out" --trace_dir "$run_dir/trace" \
    "${params[@]}" "${EXTRA[@]}" || echo "RUN FAILED: $run_id" >&2
done
```

- [ ] **Step 2: Make it executable and syntax-check it**

Run: `chmod +x benchmarks/run_sweep.sh && bash -n benchmarks/run_sweep.sh && echo OK`
Expected: prints `OK` (no syntax errors).

- [ ] **Step 3: Write the README**

Create `benchmarks/README.md`:

```markdown
# Mirage Benchmarking

Quantifies resource usage vs input size + pipeline parameters, and registration
accuracy (classic vs tiled) against landmark ground truth. See the design spec:
`docs/superpowers/specs/2026-06-16-benchmarking-framework-design.md`.

## Resource sweep

1. Generate the input matrix from your own source image:

       python benchmarks/generate_matrix.py --source /path/to/source.ome.tif --outdir bench_matrix

2. Expand the sweep into a run plan:

       python benchmarks/build_run_plan.py --sweep benchmarks/configs/sweep.yaml --out bench_run_plan.csv

3. Launch the sweep (one pipeline run per row; per-run trace + size logs isolated):

       benchmarks/run_sweep.sh bench_run_plan.csv bench_matrix/matrix_manifest.csv bench_results

Each run writes `bench_results/<run_id>/trace/trace.txt` and
`bench_results/<run_id>/out/size_logs/input_sizes.csv`, joined by the analysis notebook (Plan 3).

## Registration accuracy (Plan 2)

ANHIR is account-gated; ACROBAT landmarks are challenge-gated. Download with your own
account, then point `benchmarks/registration_eval/prepare_pairs.py` at the local data dir.
Steps live in `benchmarks/registration_eval/README.md`.
```

- [ ] **Step 4: Commit**

```bash
git add benchmarks/run_sweep.sh benchmarks/README.md
git commit -m ":sparkles: sweep driver script and benchmarks README

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Wire the benchmark tests into pytest discovery

**Files:**
- Create: `benchmarks/__init__.py`, `benchmarks/tests/__init__.py`
- Verify: full benchmark test suite runs from repo root.

- [ ] **Step 1: Create package markers so `from benchmarks...` imports resolve from repo root**

```bash
touch benchmarks/__init__.py benchmarks/tests/__init__.py
```

- [ ] **Step 2: Run the full benchmark suite from the repo root**

Run: `pytest benchmarks/tests -v`
Expected: PASS (all generator + run-plan tests green).

- [ ] **Step 3: Confirm the existing project test command is unaffected**

Run: `pytest -v tests/ --ignore=tests/testdata --ignore=tests/modules --ignore=tests/subworkflows --ignore=tests/integration 2>&1 | tail -15`
Expected: existing suite still passes (benchmark dir is separate and not pulled in by this command).

- [ ] **Step 4: Commit**

```bash
git add benchmarks/__init__.py benchmarks/tests/__init__.py
git commit -m ":white_check_mark: make benchmarks a package for pytest discovery

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage (Plan 1 portion):**
- §2 additive/gated edits → Tasks 1–3. ✓
- §4 matrix generator (size axis, channel synth, manifest, OME channel names) → Tasks 4–5. ✓
- §4.1 parameter sweep (`sweep.yaml`, ofat/grid, `run_plan.csv` with `run_id`/`varied_axis`) → Tasks 6–7. ✓
- §6 size-log relocation + `enable_size_logs` gating → Tasks 1–2. ✓
- Deferred to later plans (correctly out of scope here): §5 evaluator (Plan 2), §7–8 notebook + optimal config (Plan 3), §9 docs (Plan 4).

**Placeholder scan:** none — every code/step is concrete.

**Type consistency:** `compute_target_shape`/`synthesize_channels`/`run_matrix` signatures match between Tasks 4–5; `build_run_plan(sweep: dict) -> list[dict]` and the `run_id`/`varied_axis`/`target_px`/`n_channels` column names are consistent between Task 6 (builder) and Task 7 (driver awk/`cols` parsing); manifest columns (`cell_id…path`, path at column 7) match between Task 5 writer and Task 7 `awk '$7'`.

---

## Remaining plans (to be written after Plan 1 executes and fixes the data contracts)

- **Plan 2 — Registration-accuracy evaluator:** `landmarks.py` + ANHIR/ACROBAT adapters, `prepare_pairs.py`, the `register.py` change to publish `*_summary.csv` + `*_registrar.pickle`, `eval_tre.py` (warp landmarks → TRE/rTRE/µm), `sampling.py` three-way comparison. Synthetic landmark fixture for CI.
- **Plan 3 — Analysis notebook + optimal config:** `analysis/lib/{parse,regress,sampling,plotting}.py`, `benchmark_analysis.ipynb` (5 sections), `emit_config.py` → `conf/modules.optimized.config`. Reads Plan 1/2 schemas; runs on synthetic fixture in CI.
- **Plan 4 — Docs:** `docs/benchmarks.md` + `mkdocs.yml` nav entry, embed exported figures via glightbox.
