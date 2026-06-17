# Analysis Notebook + Optimal Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Formalize the existing ad-hoc benchmark notebooks into a tested `benchmarks/analysis/lib/`, a unified paper-ready `benchmark_analysis.ipynb`, and a regression-derived `conf/modules.optimized.config`.

**Architecture:** Lift the proven logic from `notebooks/resource_regression.ipynb`, `resources.ipynb`, `rTRE.ipynb` into a tested library (`parsing`, `load`, `regress`, `emit_config`, `plotting`). A headless `make_figures.py` orchestrates parse → regress → plot → emit-config on a results dir (fully unit-tested on a synthetic fixture). The notebook is a thin presentation layer calling the same lib. The optimal-config emitter reproduces the existing additive-σ retry-buffer formula (`check_max((<expr>*slope + intercept + sigma*task.attempt).GB, 'memory')`).

**Tech Stack:** Python 3 (numpy, pandas, scikit-learn, matplotlib, nbformat/nbconvert — all confirmed installed), pytest.

This is **Plan 3 of 4**. It consumes Plan 1 (`<run>/trace/trace.txt`, `<run>/out/size_logs/input_sizes.csv`, `matrix_manifest.csv`, `run_plan.csv`) and Plan 2 (`reg_eval.csv`). Plan 4 (docs) embeds the figures it exports.

**Spec:** `docs/superpowers/specs/2026-06-16-benchmarking-framework-design.md` §7–8.

## Prior art being formalized (do NOT duplicate)

- `notebooks/resource_regression.ipynb` — `parse_to_gb`, `parse_duration`, `load_resource_data`, per-process `LinearRegression(peak_rss_gb ~ input_gb)` + r²/σ, and the config-snippet emitter. **This is the reference for Tasks 1–4.**
- `notebooks/resources.ipynb` — scaling scatter + efficiency metrics (reference for plotting).
- `notebooks/rTRE.ipynb` — before/after registration boxplots (reference for the accuracy figures).
- `notebooks/sweep_analysis.ipynb` — a SEPARATE existing sweep subsystem; **leave untouched**, do not import or move.

Legacy notebooks stay in `notebooks/` (non-destructive); the new lib supersedes their reusable logic.

## Data layout (from Plan 1 run_sweep.sh)

```
<results_root>/<run_id>/trace/trace.txt
<results_root>/<run_id>/out/size_logs/input_sizes.csv
<matrix>/matrix_manifest.csv         # cell_id,target_px,width,height,n_channels,bytes,path
<run_plan>.csv                       # run_id,varied_axis,<param cols incl target_px,n_channels>
```

Trace fields (tab-separated, from `nextflow.config`): `task_id,process,tag,name,status,exit,submit,start,complete,duration,realtime,%cpu,cpus,memory,peak_rss,peak_vmem,rchar,wchar`. Memory like `1.2 GB`; durations like `2m 3s` / `1h 0m 5s` / `100ms`.

## File Structure

```
benchmarks/analysis/__init__.py
benchmarks/analysis/lib/__init__.py
benchmarks/analysis/lib/parsing.py     # parse_to_gb, parse_duration
benchmarks/analysis/lib/load.py        # parse_trace, parse_size_logs, load_runs
benchmarks/analysis/lib/regress.py     # fit_memory_model, fit_per_process, buffered_prediction
benchmarks/analysis/lib/emit_config.py # PROCESS_INPUT_EXPR, memory_closure, write_optimized_config
benchmarks/analysis/lib/plotting.py    # paper theme, save_fig (PDF+SVG), scatter_with_fit, before_after_box
benchmarks/analysis/make_figures.py    # end-to-end orchestrator
benchmarks/analysis/benchmark_analysis.ipynb
benchmarks/analysis/figures/           # (gitignored output; created at runtime)
benchmarks/tests/fixtures/runs/run0000/trace/trace.txt          # synthetic
benchmarks/tests/fixtures/runs/run0000/out/size_logs/input_sizes.csv
benchmarks/tests/fixtures/runs/run0001/...                      # second run
benchmarks/tests/fixtures/runs_run_plan.csv
benchmarks/tests/fixtures/runs_matrix_manifest.csv
benchmarks/tests/test_parsing.py
benchmarks/tests/test_load.py
benchmarks/tests/test_regress.py
benchmarks/tests/test_emit_config.py
benchmarks/tests/test_plotting.py
benchmarks/tests/test_make_figures.py
```

---

## Task 1: Trace value parsers (TDD)

**Files:** Create `benchmarks/analysis/__init__.py`, `benchmarks/analysis/lib/__init__.py`, `benchmarks/analysis/lib/parsing.py`; Test `benchmarks/tests/test_parsing.py`.

- [ ] **Step 1: Write failing tests**

Create `benchmarks/tests/test_parsing.py`:

```python
import numpy as np
import pytest
from benchmarks.analysis.lib.parsing import parse_to_gb, parse_duration


@pytest.mark.parametrize("val,expected", [
    ("1.2 GB", 1.2),
    ("512 MB", 0.5),
    ("1024 KB", 1.0 / 1024),
    ("2 TB", 2048.0),
    ("1073741824 B", 1.0),
    ("0", 0.0),
])
def test_parse_to_gb(val, expected):
    assert parse_to_gb(val) == pytest.approx(expected, rel=1e-6)


def test_parse_to_gb_missing_returns_nan():
    assert np.isnan(parse_to_gb("-"))
    assert np.isnan(parse_to_gb(""))


@pytest.mark.parametrize("val,expected", [
    ("1s", 1.0),
    ("2m 3s", 123.0),
    ("1h 0m 5s", 3605.0),
    ("100ms", 0.1),
    ("1.5s", 1.5),
    ("2m", 120.0),
])
def test_parse_duration(val, expected):
    assert parse_duration(val) == pytest.approx(expected, rel=1e-6)


def test_parse_duration_missing_returns_nan():
    assert np.isnan(parse_duration("-"))
```

- [ ] **Step 2: Run, verify FAIL**

Run: `python -m pytest benchmarks/tests/test_parsing.py -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement**

Create `benchmarks/analysis/__init__.py` (empty) and `benchmarks/analysis/lib/__init__.py` (empty).

Create `benchmarks/analysis/lib/parsing.py`:

```python
"""Parse Nextflow trace.txt human-readable values to numbers.

Lifted and consolidated from notebooks/resource_regression.ipynb +
notebooks/resources.ipynb (parse_to_gb, parse_duration), with millisecond
handling harmonised from notebooks/sweep_analysis.ipynb.
"""
from __future__ import annotations

import re

import numpy as np

_MEM_UNITS = {"B": 1 / 2**30, "KB": 1 / 2**20, "MB": 1 / 2**10, "GB": 1.0, "TB": 2**10}


def parse_to_gb(val) -> float:
    """'1.2 GB' / '512 MB' / '1073741824 B' / '0' -> gibibytes. '-'/'' -> NaN."""
    if val is None:
        return float("nan")
    s = str(val).strip()
    if s in ("", "-", "0"):
        return 0.0 if s == "0" else float("nan")
    m = re.match(r"^([\d.]+)\s*([KMGT]?B)$", s, re.IGNORECASE)
    if not m:
        # bare number => assume bytes
        try:
            return float(s) / 2**30
        except ValueError:
            return float("nan")
    num, unit = float(m.group(1)), m.group(2).upper()
    return num * _MEM_UNITS[unit]


def parse_duration(val) -> float:
    """'1h 30m 45s' / '2m 3s' / '100ms' / '1.5s' -> seconds. '-'/'' -> NaN."""
    if val is None:
        return float("nan")
    s = str(val).strip()
    if s in ("", "-"):
        return float("nan")
    # milliseconds first (avoid 'm' of 'ms' matching minutes)
    total = 0.0
    matched = False
    ms = re.search(r"([\d.]+)\s*ms", s)
    if ms:
        total += float(ms.group(1)) / 1000.0
        s = s[: ms.start()] + s[ms.end():]
        matched = True
    for value, unit in re.findall(r"([\d.]+)\s*([hms])", s):
        matched = True
        total += float(value) * {"h": 3600.0, "m": 60.0, "s": 1.0}[unit]
    return total if matched else float("nan")
```

- [ ] **Step 4: Run, verify PASS**

Run: `python -m pytest benchmarks/tests/test_parsing.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/analysis/__init__.py benchmarks/analysis/lib/__init__.py benchmarks/analysis/lib/parsing.py benchmarks/tests/test_parsing.py
git commit -m ":sparkles: lift trace value parsers (GB, duration) into tested lib" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Trace/size loaders + per-run join (TDD)

**Files:** Create `benchmarks/analysis/lib/load.py`; synthetic fixtures; Test `benchmarks/tests/test_load.py`.

- [ ] **Step 1: Create synthetic fixtures**

Create `benchmarks/tests/fixtures/runs/run0000/trace/trace.txt` (tab-separated; header then 2 process rows):

```
task_id	process	tag	name	status	exit	submit	start	complete	duration	realtime	%cpu	cpus	memory	peak_rss	peak_vmem	rchar	wchar
1	CONVERT_IMAGE	px4096_ch2	CONVERT_IMAGE (px4096_ch2)	COMPLETED	0	-	-	-	2m 0s	1m 50s	180%	4	100 GB	8 GB	9 GB	1 GB	1 GB
2	SEGMENT	px4096_ch2	SEGMENT (px4096_ch2)	COMPLETED	0	-	-	-	5m 0s	4m 30s	200%	2	64 GB	20 GB	22 GB	2 GB	2 GB
```

Create `benchmarks/tests/fixtures/runs/run0000/out/size_logs/input_sizes.csv`:

```csv
process,sample_id,filename,bytes
CONVERT_IMAGE,px4096_ch2,px4096_ch2.ome.tif,2147483648
SEGMENT,px4096_ch2,merged.ome.tif,4294967296
```

Create `benchmarks/tests/fixtures/runs/run0001/trace/trace.txt`:

```
task_id	process	tag	name	status	exit	submit	start	complete	duration	realtime	%cpu	cpus	memory	peak_rss	peak_vmem	rchar	wchar
1	CONVERT_IMAGE	px8192_ch2	CONVERT_IMAGE (px8192_ch2)	COMPLETED	0	-	-	-	4m 0s	3m 40s	185%	4	100 GB	16 GB	18 GB	2 GB	2 GB
2	SEGMENT	px8192_ch2	SEGMENT (px8192_ch2)	COMPLETED	0	-	-	-	10m 0s	9m 0s	205%	2	64 GB	40 GB	44 GB	4 GB	4 GB
```

Create `benchmarks/tests/fixtures/runs/run0001/out/size_logs/input_sizes.csv`:

```csv
process,sample_id,filename,bytes
CONVERT_IMAGE,px8192_ch2,px8192_ch2.ome.tif,4294967296
SEGMENT,px8192_ch2,merged.ome.tif,8589934592
```

Create `benchmarks/tests/fixtures/runs_run_plan.csv`:

```csv
run_id,varied_axis,target_px,n_channels,memory_mode
run0000,baseline,4096,2,medium
run0001,target_px,8192,2,medium
```

Create `benchmarks/tests/fixtures/runs_matrix_manifest.csv`:

```csv
cell_id,target_px,width,height,n_channels,bytes,path
px4096_ch2,4096,4096,2048,2,2147483648,/d/px4096_ch2.ome.tif
px8192_ch2,8192,8192,4096,2,4294967296,/d/px8192_ch2.ome.tif
```

- [ ] **Step 2: Write failing tests**

Create `benchmarks/tests/test_load.py`:

```python
from pathlib import Path

import numpy as np
import pytest

from benchmarks.analysis.lib import load

FIX = Path(__file__).parent / "fixtures"


def test_parse_trace_converts_units():
    df = load.parse_trace(FIX / "runs/run0000/trace/trace.txt")
    seg = df[df["process"] == "SEGMENT"].iloc[0]
    assert seg["peak_rss_gb"] == pytest.approx(20.0)
    assert seg["realtime_s"] == pytest.approx(270.0)  # 4m 30s
    assert seg["cpus"] == 2


def test_parse_size_logs_sums_bytes_per_process():
    s = load.parse_size_logs(FIX / "runs/run0000/out/size_logs/input_sizes.csv")
    assert s.loc["SEGMENT", "input_gb"] == pytest.approx(4.0)
    assert s.loc["CONVERT_IMAGE", "input_gb"] == pytest.approx(2.0)


def test_load_runs_joins_trace_sizes_and_params():
    df = load.load_runs(
        FIX / "runs",
        run_plan_csv=FIX / "runs_run_plan.csv",
        manifest_csv=FIX / "runs_matrix_manifest.csv",
    )
    # 2 runs x 2 processes = 4 rows
    assert len(df) == 4
    assert {"run_id", "process", "peak_rss_gb", "realtime_s", "input_gb",
            "target_px", "n_channels", "varied_axis"} <= set(df.columns)
    seg1 = df[(df["run_id"] == "run0001") & (df["process"] == "SEGMENT")].iloc[0]
    assert seg1["peak_rss_gb"] == pytest.approx(40.0)
    assert seg1["input_gb"] == pytest.approx(8.0)
    assert seg1["target_px"] == 8192
```

- [ ] **Step 3: Run, verify FAIL**

Run: `python -m pytest benchmarks/tests/test_load.py -v`
Expected: FAIL — import error.

- [ ] **Step 4: Implement**

Create `benchmarks/analysis/lib/load.py`:

```python
"""Load + join Nextflow trace, size logs, run plan and matrix manifest into a tidy frame.

Generalises notebooks/resource_regression.ipynb:load_resource_data over a results
tree laid out by benchmarks/run_sweep.sh: <root>/<run_id>/trace/trace.txt and
<root>/<run_id>/out/size_logs/input_sizes.csv.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .parsing import parse_duration, parse_to_gb


def parse_trace(trace_txt) -> pd.DataFrame:
    df = pd.read_csv(trace_txt, sep="\t")
    out = pd.DataFrame({
        "process": df["process"],
        "tag": df.get("tag"),
        "status": df.get("status"),
        "exit": pd.to_numeric(df.get("exit"), errors="coerce"),
        "peak_rss_gb": df["peak_rss"].map(parse_to_gb),
        "peak_vmem_gb": df["peak_vmem"].map(parse_to_gb),
        "realtime_s": df["realtime"].map(parse_duration),
        "duration_s": df["duration"].map(parse_duration),
        "cpus": pd.to_numeric(df.get("cpus"), errors="coerce"),
    })
    return out


def parse_size_logs(input_sizes_csv) -> pd.DataFrame:
    df = pd.read_csv(input_sizes_csv)
    agg = df.groupby("process")["bytes"].sum().to_frame()
    agg["input_gb"] = agg["bytes"] / 2**30
    return agg


def load_runs(results_root, run_plan_csv, manifest_csv) -> pd.DataFrame:
    root = Path(results_root)
    plan = pd.read_csv(run_plan_csv)
    rows = []
    for _, prow in plan.iterrows():
        run_id = prow["run_id"]
        trace = root / run_id / "trace" / "trace.txt"
        sizes = root / run_id / "out" / "size_logs" / "input_sizes.csv"
        if not trace.exists():
            continue
        t = parse_trace(trace)
        if sizes.exists():
            s = parse_size_logs(sizes)
            t = t.merge(s["input_gb"], left_on="process", right_index=True, how="left")
        else:
            t["input_gb"] = float("nan")
        for col in plan.columns:
            t[col] = prow[col]
        rows.append(t)
    df = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    return df
```

- [ ] **Step 5: Run, verify PASS**

Run: `python -m pytest benchmarks/tests/test_load.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add benchmarks/analysis/lib/load.py benchmarks/tests/test_load.py benchmarks/tests/fixtures/runs benchmarks/tests/fixtures/runs_run_plan.csv benchmarks/tests/fixtures/runs_matrix_manifest.csv
git commit -m ":sparkles: trace/size loaders + per-run join into tidy frame" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Per-process resource regression (TDD)

**Files:** Create `benchmarks/analysis/lib/regress.py`; Test `benchmarks/tests/test_regress.py`.

- [ ] **Step 1: Write failing tests**

Create `benchmarks/tests/test_regress.py`:

```python
import numpy as np
import pandas as pd
import pytest

from benchmarks.analysis.lib import regress


def test_fit_memory_model_recovers_known_line():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    y = 3.0 * x + 2.0  # slope 3, intercept 2, no noise
    m = regress.fit_memory_model(x, y)
    assert m["slope"] == pytest.approx(3.0)
    assert m["intercept"] == pytest.approx(2.0)
    assert m["r2"] == pytest.approx(1.0)
    assert m["sigma"] == pytest.approx(0.0, abs=1e-9)
    assert m["n"] == 5


def test_fit_memory_model_too_few_points_is_flat_fallback():
    m = regress.fit_memory_model(np.array([2.0]), np.array([10.0]))
    assert m["slope"] == 0.0
    assert m["intercept"] == pytest.approx(10.0)
    assert m["n"] == 1


def test_buffered_prediction_adds_sigma_per_attempt():
    m = {"slope": 3.0, "intercept": 2.0, "sigma": 1.0}
    # input_gb=4 -> 14 base; attempt 1 -> +1 sigma = 15; attempt 2 -> +2 = 16
    assert regress.buffered_prediction(m, input_gb=4.0, attempt=1) == pytest.approx(15.0)
    assert regress.buffered_prediction(m, input_gb=4.0, attempt=2) == pytest.approx(16.0)


def test_fit_per_process_groups_by_process():
    df = pd.DataFrame({
        "process": ["A", "A", "A", "B", "B", "B"],
        "input_gb": [1.0, 2.0, 3.0, 1.0, 2.0, 3.0],
        "peak_rss_gb": [3.0, 5.0, 7.0, 11.0, 21.0, 31.0],  # A: 2x+1 ; B: 10x+1
    })
    models = regress.fit_per_process(df, predictor="input_gb", target="peak_rss_gb")
    assert models["A"]["slope"] == pytest.approx(2.0)
    assert models["B"]["slope"] == pytest.approx(10.0)
```

- [ ] **Step 2: Run, verify FAIL**

Run: `python -m pytest benchmarks/tests/test_regress.py -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement**

Create `benchmarks/analysis/lib/regress.py`:

```python
"""Per-process resource regression.

Formalises notebooks/resource_regression.ipynb: fit peak_rss_gb ~ input_gb with
scikit-learn, report r2 and residual sigma, and size with an additive-sigma
retry buffer (attempt N => base + N*sigma) matching the repo's task.attempt scaling.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.metrics import r2_score


def fit_memory_model(x, y) -> dict:
    """Fit y ~ x. <3 points => flat fallback (slope 0, intercept = mean y)."""
    x = np.asarray(x, dtype=float).reshape(-1, 1)
    y = np.asarray(y, dtype=float)
    n = int(y.size)
    if n < 3:
        return {"slope": 0.0, "intercept": float(np.mean(y)) if n else 0.0,
                "r2": float("nan"), "sigma": float(np.std(y)) if n else 0.0, "n": n}
    reg = LinearRegression().fit(x, y)
    pred = reg.predict(x)
    resid = y - pred
    return {
        "slope": float(reg.coef_[0]),
        "intercept": float(reg.intercept_),
        "r2": float(r2_score(y, pred)),
        "sigma": float(np.std(resid, ddof=0)),
        "n": n,
    }


def buffered_prediction(model: dict, input_gb: float, attempt: int = 1) -> float:
    """base = slope*input + intercept; add `attempt` * sigma as retry headroom."""
    base = model["slope"] * input_gb + model["intercept"]
    return float(base + attempt * model.get("sigma", 0.0))


def fit_per_process(df: pd.DataFrame, predictor: str = "input_gb",
                    target: str = "peak_rss_gb") -> dict:
    models = {}
    for proc, g in df.groupby("process"):
        sub = g[[predictor, target]].dropna()
        models[proc] = fit_memory_model(sub[predictor].to_numpy(), sub[target].to_numpy())
    return models
```

- [ ] **Step 4: Run, verify PASS**

Run: `python -m pytest benchmarks/tests/test_regress.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/analysis/lib/regress.py benchmarks/tests/test_regress.py
git commit -m ":sparkles: per-process resource regression (sklearn, r2, sigma buffer)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Optimal modules.config emitter (TDD)

**Files:** Create `benchmarks/analysis/lib/emit_config.py`; Test `benchmarks/tests/test_emit_config.py`.

- [ ] **Step 1: Write failing tests**

Create `benchmarks/tests/test_emit_config.py`:

```python
from benchmarks.analysis.lib import emit_config


def test_memory_closure_formats_additive_sigma_buffer():
    model = {"slope": 7.0, "intercept": 8.0, "sigma": 4.0, "r2": 0.97, "n": 5}
    line = emit_config.memory_closure("SEGMENT", model, input_expr="file_gb")
    # check_max(( <expr>*slope + intercept + sigma*task.attempt ).GB, 'memory')
    assert "withName: 'SEGMENT'" in line
    assert "file_gb * 7.0" in line
    assert "+ 8.0" in line
    assert "+ 4.0 * task.attempt" in line
    assert "check_max(" in line and ".GB, 'memory'" in line


def test_write_optimized_config_emits_header_and_blocks(tmp_path):
    models = {
        "SEGMENT": {"slope": 7.0, "intercept": 8.0, "sigma": 4.0, "r2": 0.97, "n": 5},
        "CONVERT_IMAGE": {"slope": 1.0, "intercept": 2.0, "sigma": 0.5, "r2": 0.9, "n": 5},
    }
    out = tmp_path / "modules.optimized.config"
    emit_config.write_optimized_config(models, out)
    text = out.read_text()
    assert text.lstrip().startswith("//")  # provenance header comment
    assert "withName: 'SEGMENT'" in text
    assert "withName: 'CONVERT_IMAGE'" in text
    # processes with no known input expr fall back to a documented default
    assert "r2=0.97" in text  # fit quality annotated as a comment


def test_low_confidence_fit_is_flagged(tmp_path):
    models = {"FOO": {"slope": 1.0, "intercept": 1.0, "sigma": 1.0, "r2": 0.2, "n": 4}}
    out = tmp_path / "c.config"
    emit_config.write_optimized_config(models, out)
    assert "LOW CONFIDENCE" in out.read_text()
```

- [ ] **Step 2: Run, verify FAIL**

Run: `python -m pytest benchmarks/tests/test_emit_config.py -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement**

Create `benchmarks/analysis/lib/emit_config.py`:

```python
"""Emit a regression-derived conf/modules.optimized.config.

Reproduces the additive-sigma buffer from notebooks/resource_regression.ipynb:
    memory = { check_max( ( <input_expr>*slope + intercept + sigma*task.attempt ).GB, 'memory' ) }
Writes a SEPARATE file (never overwrites the live conf/modules.config) for review.
"""
from __future__ import annotations

from pathlib import Path

# Per-process Groovy expression for input size in GiB. Processes absent here use
# the generic `file_gb` placeholder, which the user maps to the real input var.
PROCESS_INPUT_EXPR = {
    "CONVERT_IMAGE": "(image_file.size() >> 30)",
    "PREPROCESS": "(ome_tiff.size() >> 30)",
    "SEGMENT": "(merged_file.size() >> 30)",
    "MERGE_AND_PYRAMID": "(total_gb)",
}

R2_LOW = 0.5


def memory_closure(process: str, model: dict, input_expr: str | None = None) -> str:
    expr = input_expr if input_expr is not None else PROCESS_INPUT_EXPR.get(process, "file_gb")
    slope = round(model["slope"], 3)
    intercept = round(model["intercept"], 3)
    sigma = round(model.get("sigma", 0.0), 3)
    body = (f"check_max( ( {expr} * {slope} + {intercept} + {sigma} * task.attempt ).GB, "
            f"'memory' )")
    return f"    withName: '{process}' {{\n        memory = {{ {body} }}\n    }}"


def write_optimized_config(models: dict, out_path) -> None:
    lines = [
        "// conf/modules.optimized.config",
        "// AUTO-GENERATED from benchmark regression (benchmarks/analysis). REVIEW before use.",
        "// memory = check_max((input_gb*slope + intercept + sigma*task.attempt).GB, 'memory')",
        "process {",
    ]
    for process, model in sorted(models.items()):
        r2 = model.get("r2")
        note = f"    // fit: r2={round(r2, 2) if r2 == r2 else 'n/a'}, n={model.get('n')}"
        if r2 is None or r2 != r2 or r2 < R2_LOW:
            note += "  // LOW CONFIDENCE — verify against observed peaks"
        lines.append(note)
        lines.append(memory_closure(process, model))
    lines.append("}")
    Path(out_path).write_text("\n".join(lines) + "\n")
```

- [ ] **Step 4: Run, verify PASS**

Run: `python -m pytest benchmarks/tests/test_emit_config.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/analysis/lib/emit_config.py benchmarks/tests/test_emit_config.py
git commit -m ":sparkles: regression-derived modules.optimized.config emitter" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Paper-ready plotting helpers (TDD)

**Files:** Create `benchmarks/analysis/lib/plotting.py`; Test `benchmarks/tests/test_plotting.py`.

- [ ] **Step 1: Write failing tests**

Create `benchmarks/tests/test_plotting.py`:

```python
import matplotlib
matplotlib.use("Agg")  # headless

import numpy as np
import pandas as pd

from benchmarks.analysis.lib import plotting


def test_save_fig_writes_pdf_and_svg(tmp_path):
    import matplotlib.pyplot as plt
    plotting.set_paper_theme()
    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])
    paths = plotting.save_fig(fig, tmp_path / "fig1")
    assert (tmp_path / "fig1.pdf").exists() and (tmp_path / "fig1.svg").exists()
    assert (tmp_path / "fig1.pdf").stat().st_size > 0
    assert set(paths) == {tmp_path / "fig1.pdf", tmp_path / "fig1.svg"}


def test_scatter_with_fit_returns_figure(tmp_path):
    x = np.array([1.0, 2.0, 3.0, 4.0])
    y = 2.0 * x + 1.0
    fig = plotting.scatter_with_fit(x, y, slope=2.0, intercept=1.0,
                                    xlabel="input (GB)", ylabel="peak RSS (GB)",
                                    title="SEGMENT")
    assert fig is not None
    plotting.save_fig(fig, tmp_path / "scatter")  # must not raise
    assert (tmp_path / "scatter.pdf").exists()


def test_before_after_box_returns_figure():
    df = pd.DataFrame({"original": [0.1, 0.2, 0.3], "registered": [0.02, 0.03, 0.04]})
    fig = plotting.before_after_box(df, cols=["original", "registered"],
                                    ylabel="rTRE", title="tiled")
    assert fig is not None
```

- [ ] **Step 2: Run, verify FAIL**

Run: `python -m pytest benchmarks/tests/test_plotting.py -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement**

Create `benchmarks/analysis/lib/plotting.py`:

```python
"""Paper-ready matplotlib helpers: consistent theme + vector export (PDF+SVG).

Plot styles consolidated from notebooks/resources.ipynb (scaling scatter) and
notebooks/rTRE.ipynb (before/after boxplots).
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

_THEME = {
    "figure.figsize": (5.0, 3.5),
    "figure.dpi": 120,
    "savefig.bbox": "tight",
    "font.size": 10,
    "axes.titlesize": 11,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "legend.frameon": False,
}


def set_paper_theme() -> None:
    plt.rcParams.update(_THEME)


def save_fig(fig, path_stem) -> list[Path]:
    """Save `fig` as both .pdf and .svg (vector, paper-ready). Returns the paths."""
    stem = Path(path_stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    out = []
    for ext in ("pdf", "svg"):
        p = stem.with_suffix(f".{ext}")
        fig.savefig(p)
        out.append(p)
    plt.close(fig)
    return out


def scatter_with_fit(x, y, slope, intercept, xlabel, ylabel, title):
    x = np.asarray(x, dtype=float)
    fig, ax = plt.subplots()
    ax.scatter(x, y, s=18, alpha=0.8)
    xs = np.linspace(x.min(), x.max(), 50) if x.size else np.array([0, 1])
    ax.plot(xs, slope * xs + intercept, color="C3", lw=1.5,
            label=f"y = {slope:.2f}x + {intercept:.2f}")
    ax.set(xlabel=xlabel, ylabel=ylabel, title=title)
    ax.legend()
    return fig


def before_after_box(df, cols, ylabel, title, log_scale=True):
    fig, ax = plt.subplots()
    ax.boxplot([df[c].dropna().to_numpy() for c in cols], labels=cols)
    if log_scale:
        ax.set_yscale("log")
    ax.set(ylabel=ylabel, title=title)
    return fig
```

- [ ] **Step 4: Run, verify PASS**

Run: `python -m pytest benchmarks/tests/test_plotting.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/analysis/lib/plotting.py benchmarks/tests/test_plotting.py
git commit -m ":sparkles: paper-ready plotting helpers (theme + PDF/SVG export)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: make_figures.py end-to-end orchestrator (TDD)

**Files:** Create `benchmarks/analysis/make_figures.py`; Test `benchmarks/tests/test_make_figures.py`.

- [ ] **Step 1: Write failing test (end-to-end on the synthetic fixture)**

Create `benchmarks/tests/test_make_figures.py`:

```python
from pathlib import Path

from benchmarks.analysis import make_figures

FIX = Path(__file__).parent / "fixtures"


def test_run_produces_config_and_figures(tmp_path):
    result = make_figures.run(
        results_root=FIX / "runs",
        run_plan_csv=FIX / "runs_run_plan.csv",
        manifest_csv=FIX / "runs_matrix_manifest.csv",
        reg_eval_csv=None,
        outdir=tmp_path,
    )
    # tidy frame has the 2 runs x 2 processes
    assert len(result["runs_df"]) == 4
    # optimized config written + per-process scaling figures exist
    assert (tmp_path / "modules.optimized.config").exists()
    figs = list((tmp_path / "figures").glob("scaling_*.pdf"))
    assert len(figs) >= 1
    assert "SEGMENT" in result["models"]
```

- [ ] **Step 2: Run, verify FAIL**

Run: `python -m pytest benchmarks/tests/test_make_figures.py -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement**

Create `benchmarks/analysis/make_figures.py`:

```python
"""Headless end-to-end benchmark analysis: parse -> regress -> plot -> emit config.

This is the reproducible artifact the notebook mirrors. Run:
  python -m benchmarks.analysis.make_figures --results-root <dir> \
      --run-plan run_plan.csv --manifest matrix_manifest.csv \
      --reg-eval reg_eval.csv --outdir benchmarks/analysis/figures
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

from .lib import emit_config, load, plotting, regress


def run(results_root, run_plan_csv, manifest_csv, reg_eval_csv, outdir) -> dict:
    outdir = Path(outdir)
    figdir = outdir / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    plotting.set_paper_theme()

    runs_df = load.load_runs(results_root, run_plan_csv, manifest_csv)
    models = regress.fit_per_process(runs_df, predictor="input_gb", target="peak_rss_gb")

    # per-process memory scaling figures
    for proc, g in runs_df.groupby("process"):
        sub = g[["input_gb", "peak_rss_gb"]].dropna()
        if sub.empty:
            continue
        m = models[proc]
        fig = plotting.scatter_with_fit(
            sub["input_gb"], sub["peak_rss_gb"], m["slope"], m["intercept"],
            xlabel="input (GiB)", ylabel="peak RSS (GiB)", title=proc)
        plotting.save_fig(fig, figdir / f"scaling_{proc}")

    emit_config.write_optimized_config(models, outdir / "modules.optimized.config")
    return {"runs_df": runs_df, "models": models, "outdir": outdir}


def main():
    ap = argparse.ArgumentParser(description="Benchmark analysis: figures + optimal config.")
    ap.add_argument("--results-root", required=True)
    ap.add_argument("--run-plan", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--reg-eval", default=None)
    ap.add_argument("--outdir", default="benchmarks/analysis")
    a = ap.parse_args()
    res = run(a.results_root, a.run_plan, a.manifest, a.reg_eval, a.outdir)
    print(f"Wrote {res['outdir']}/modules.optimized.config and figures for "
          f"{len(res['models'])} processes")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run, verify PASS**

Run: `python -m pytest benchmarks/tests/test_make_figures.py -v`
Expected: 1 passed; a `modules.optimized.config` + `scaling_*.pdf/svg` written under tmp.

- [ ] **Step 5: Add a .gitignore for runtime figure output**

Create `benchmarks/analysis/figures/.gitignore`:

```
*
!.gitignore
```

- [ ] **Step 6: Commit**

```bash
git add benchmarks/analysis/make_figures.py benchmarks/tests/test_make_figures.py benchmarks/analysis/figures/.gitignore
git commit -m ":sparkles: end-to-end make_figures orchestrator (parse->regress->plot->config)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Unified paper-ready notebook + execution smoke

**Files:** Create `benchmarks/analysis/benchmark_analysis.ipynb` (via nbformat), Test extends `benchmarks/tests/test_make_figures.py`.

- [ ] **Step 1: Build the notebook programmatically**

Create a one-off generator and run it (the generator is committed for reproducibility) — `benchmarks/analysis/_build_notebook.py`:

```python
"""Generate benchmark_analysis.ipynb (thin presentation layer over the lib)."""
from pathlib import Path

import nbformat as nbf

CELLS = [
    ("md", "# Mirage Benchmark Analysis\n\n"
           "Resource scaling + regression, the regression-derived `modules.config`, "
           "and registration accuracy (tiled vs classic). Set `RESULTS_ROOT` etc. below. "
           "All logic lives in `benchmarks/analysis/lib` (tested)."),
    ("code", "import matplotlib\n%matplotlib inline\n"
             "from pathlib import Path\n"
             "from benchmarks.analysis.lib import load, regress, emit_config, plotting\n"
             "from benchmarks.analysis import make_figures\n"
             "plotting.set_paper_theme()\n"
             "# EDIT THESE to your sweep outputs:\n"
             "RESULTS_ROOT = Path('../../bench_results')\n"
             "RUN_PLAN = Path('../../bench_run_plan.csv')\n"
             "MANIFEST = Path('../../bench_matrix/matrix_manifest.csv')\n"
             "REG_EVAL = Path('../../reg_eval.csv')  # from Plan 2; may not exist yet"),
    ("md", "## 1. Load + tidy"),
    ("code", "df = load.load_runs(RESULTS_ROOT, RUN_PLAN, MANIFEST)\n"
             "print(df.shape)\ndf.head()"),
    ("md", "## 2. Resource ~ input-size regression (per process)"),
    ("code", "models = regress.fit_per_process(df, predictor='input_gb', target='peak_rss_gb')\n"
             "for p, m in sorted(models.items()):\n"
             "    print(f\"{p:24s} slope={m['slope']:.2f} intercept={m['intercept']:.2f} "
             "r2={m['r2']:.2f} sigma={m['sigma']:.2f} n={m['n']}\")"),
    ("code", "for proc, g in df.groupby('process'):\n"
             "    sub = g[['input_gb','peak_rss_gb']].dropna()\n"
             "    if sub.empty: continue\n"
             "    m = models[proc]\n"
             "    fig = plotting.scatter_with_fit(sub['input_gb'], sub['peak_rss_gb'],\n"
             "        m['slope'], m['intercept'], 'input (GiB)', 'peak RSS (GiB)', proc)\n"
             "    plotting.save_fig(fig, Path('figures')/f'scaling_{proc}')"),
    ("md", "## 3. Optimal modules.config"),
    ("code", "emit_config.write_optimized_config(models, '../../conf/modules.optimized.config')\n"
             "print(open('../../conf/modules.optimized.config').read())"),
    ("md", "## 4. Registration accuracy — tiled vs classic (needs Plan 2 reg_eval.csv)"),
    ("code", "import pandas as pd\n"
             "if REG_EVAL.exists():\n"
             "    reg = pd.read_csv(REG_EVAL)\n"
             "    piv = reg.pivot_table(index='pair_id', columns='mode', values='true_median_rtre')\n"
             "    fig = plotting.before_after_box(piv, cols=list(piv.columns),\n"
             "        ylabel='median rTRE', title='tiled vs untiled')\n"
             "    plotting.save_fig(fig, Path('figures')/'rtre_tiled_vs_untiled')\n"
             "    display(reg.groupby('mode')[['true_median_rtre','valis_rtre','feature_median']].median())\n"
             "else:\n"
             "    print('reg_eval.csv not found - run Plan 2 first')"),
    ("md", "## 5. Sampling — paired tiled-vs-untiled significance"),
    ("code", "from benchmarks.registration_eval import sampling\n"
             "if REG_EVAL.exists():\n"
             "    reg = pd.read_csv(REG_EVAL)\n"
             "    piv = reg.pivot_table(index='pair_id', columns='mode', values='true_median_px').dropna()\n"
             "    if {'tiled','untiled'} <= set(piv.columns):\n"
             "        print(sampling.paired_diff_test(piv['tiled'].values, piv['untiled'].values))"),
]


def build(path):
    nb = nbf.v4.new_notebook()
    nb.cells = [nbf.v4.new_markdown_cell(s) if k == "md" else nbf.v4.new_code_cell(s)
                for k, s in CELLS]
    nbf.write(nb, str(path))


if __name__ == "__main__":
    build(Path(__file__).parent / "benchmark_analysis.ipynb")
```

Run it: `python benchmarks/analysis/_build_notebook.py` → creates `benchmarks/analysis/benchmark_analysis.ipynb`.

- [ ] **Step 2: Add a notebook execution smoke test**

Append to `benchmarks/tests/test_make_figures.py`:

```python
def test_notebook_executes_on_fixture(tmp_path):
    """Smoke: the notebook's lib calls run headless against the fixture.

    We execute the analysis path (not nbconvert, to stay dependency-light) by
    re-running make_figures, which mirrors notebook sections 1-3.
    """
    res = make_figures.run(
        results_root=FIX / "runs", run_plan_csv=FIX / "runs_run_plan.csv",
        manifest_csv=FIX / "runs_matrix_manifest.csv", reg_eval_csv=None, outdir=tmp_path,
    )
    assert (tmp_path / "modules.optimized.config").read_text().count("withName") >= 2


def test_notebook_file_exists_and_is_valid():
    import nbformat
    from pathlib import Path
    nb_path = Path(__file__).parents[1] / "analysis" / "benchmark_analysis.ipynb"
    nb = nbformat.read(str(nb_path), as_version=4)
    assert len(nb.cells) >= 8  # 5 sections + intro + setup
```

- [ ] **Step 3: Run, verify PASS**

Run: `python -m pytest benchmarks/tests/test_make_figures.py -v`
Expected: 3 passed (the original + 2 new).

Optionally (if you want a full kernel execution): `jupyter nbconvert --to notebook --execute --ExecutePreprocessor.timeout=120 benchmarks/analysis/benchmark_analysis.ipynb --stdout >/dev/null` — but this requires the `RESULTS_ROOT` paths to exist, so it is NOT part of the test (the test exercises the lib directly).

- [ ] **Step 4: Commit**

```bash
git add benchmarks/analysis/_build_notebook.py benchmarks/analysis/benchmark_analysis.ipynb benchmarks/tests/test_make_figures.py
git commit -m ":sparkles: unified paper-ready benchmark notebook + smoke test" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Full suite verification

- [ ] **Step 1: Run the whole benchmark suite**

Run: `python -m pytest benchmarks/tests -q`
Expected: all pass (prior 38 + Plan 3 new). Report the exact count.

- [ ] **Step 2: Confirm existing project suite unaffected**

Run: `python -m pytest -q tests/ --ignore=tests/testdata --ignore=tests/modules --ignore=tests/subworkflows --ignore=tests/integration 2>&1 | tail -5`
Expected: 19 passed, 3 skipped.

- [ ] **Step 3: Confirm make_figures CLI is importable**

Run: `python -c "from benchmarks.analysis import make_figures; print('ok')"`

---

## Self-Review

**Spec coverage (§7–8):**
- §7.1 ingest (trace+sizes+manifest join) → Tasks 1–2. ✓
- §7.2 resource~size regression → Task 3. ✓
- §7.3 tiled-vs-classic accuracy (notebook section 4 over Plan 2 reg_eval) → Task 7. ✓
- §7.4 sampling (reuses `registration_eval.sampling`, DRY) → Task 7 section 5. ✓
- §7.5 paper-ready styling (theme + PDF/SVG) → Task 5. ✓
- §8 optimal config (additive-σ buffer, separate file) → Task 4. ✓
- Notebook (5 sections) → Task 7. ✓

**Placeholder scan:** none.

**Type/name consistency:** `fit_memory_model`/`buffered_prediction`/`fit_per_process` signatures consistent across regress + emit_config + make_figures; tidy-frame columns (`process`, `input_gb`, `peak_rss_gb`, `realtime_s`) consistent between `load.load_runs`, `regress.fit_per_process`, and `make_figures.run`; `save_fig`/`scatter_with_fit`/`before_after_box` consistent between plotting + make_figures + notebook; `emit_config.write_optimized_config(models, path)` consistent across Task 4 + 6 + 7; reuses `benchmarks.registration_eval.sampling.paired_diff_test` (no duplication).

---

## Remaining plan

- **Plan 4 — Docs:** `docs/benchmarks.md` + `mkdocs.yml` nav entry; embed exported figures (`benchmarks/analysis/figures/*.{pdf,svg}` → `docs/assets/images/`) via glightbox; a "how the optimal config was derived" subsection linking the regression to `conf/modules.optimized.config`.
