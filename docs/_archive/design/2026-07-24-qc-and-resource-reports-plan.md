# QC Report Overhaul + Computational-Resources Report — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the final QC report capture *all* QC artifacts plus run context (summary, manifest, versions, status), and add a standalone computational-resources report built from the size logs and Nextflow trace.

**Architecture:** Two independently-executable parts. Part A extends the existing self-contained HTML QC report (`bin/generate_qc_report.py`) and its Nextflow wiring so three currently-dropped QC artifacts and four non-QC context sections are surfaced. Part B adds a new stdlib-only `bin/generate_resource_report.py`, invoked best-effort from `workflow.onComplete` in `main.nf`, that joins `input_sizes.csv` with `.trace/trace.txt`.

**Tech Stack:** Python 3 (stdlib only — no PyYAML, no pandas, no plotting libs), Nextflow DSL2, pytest, nf-test.

**Design reference:** `docs/design/2026-07-24-qc-and-resource-reports.md`

## Global Constraints

- Python: **stdlib only**. No new dependencies in either report script (matches the existing self-contained-HTML ethos). Verbatim: "both reports stay stdlib-only".
- Every new section/parser degrades gracefully: missing/partial input → styled "not available" notice, **never** an exception, **never** a non-zero exit that could fail the run.
- `bin/generate_resource_report.py` is invoked **by name** from `main.nf`, so it MUST be tracked `100755` (`git update-index --chmod=+x`; confirm `git ls-files -s` shows `100755`). Import-only files stay `100644`. (CLAUDE.md rule.)
- Nextflow ≥ 25.04.0, strict parser: cannot invoke a closure-typed local variable as a function; call `ParamUtils.shouldRun(...)` inline.
- Commit style: gitmoji `:shortcode:` prefix on every commit (e.g. `:sparkles:`, `:bug:`, `:white_check_mark:`).
- Concurrency: another agent may share this worktree. Before any git write, re-check `git rev-parse HEAD`; `git add` only the explicit files listed in the step — never `git add -A`.
- Trace field order (from `nextflow.config`): `task_id, process, tag, name, status, exit, submit, start, complete, duration, realtime, %cpu, cpus, memory, peak_rss, peak_vmem, rchar, wchar`.
- Size-log CSV header (from `AGGREGATE_SIZE_LOGS`): `process,sample_id,filename,bytes` where `process` = full `${task.process}` name (exact match to trace `process`).

---

# PART A — QC Report Overhaul

Files touched in Part A:
- Modify: `bin/generate_qc_report.py` — new parsers + section builders + CLI args.
- Test: `tests/test_generate_qc_report.py` (new).
- Modify: `modules/local/generate_qc_report.nf` — 3 new inputs + CLI flags + stub.
- Modify: `subworkflows/local/registration.nf` — add `distance_plots` emit.
- Modify: `workflows/mirage.nf` — build `run_summary.json`; forward distance plots + warp-seg QC; pass new inputs.
- Modify: `tests/modules/generate_qc_report.nf.test` — new positional inputs.
- Modify: `docs/parameters.md` — document the enriched report.

All Part A Python tests run with:
`pytest -v tests/test_generate_qc_report.py`

---

### Task A1: `versions.yml` parser + Software Versions section

**Files:**
- Modify: `bin/generate_qc_report.py`
- Test: `tests/test_generate_qc_report.py`

**Interfaces:**
- Produces: `parse_versions_yml(path) -> dict[str, dict[str, str]]` (process → {tool: version}); `versions_section(versions_path) -> str` (HTML section).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_generate_qc_report.py
import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "generate_qc_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("gqr", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_versions_yml_two_level(tmp_path):
    gqr = _load()
    p = tmp_path / "collated_versions.yml"
    p.write_text(
        '"MIRAGE:PREPROCESSING:CONVERT_IMAGE":\n'
        "    python: 3.10.0\n"
        '"MIRAGE:REGISTRATION:REGISTER":\n'
        "    python: 3.10.0\n"
        "    valis: 1.0.0\n"
    )
    out = gqr.parse_versions_yml(p)
    assert out["MIRAGE:PREPROCESSING:CONVERT_IMAGE"]["python"] == "3.10.0"
    assert out["MIRAGE:REGISTRATION:REGISTER"]["valis"] == "1.0.0"


def test_versions_section_renders_table(tmp_path):
    gqr = _load()
    p = tmp_path / "v.yml"
    p.write_text('"A:B":\n    tool: 1.2.3\n')
    html = gqr.versions_section(p)
    assert "Software Versions" in html
    assert "tool" in html and "1.2.3" in html


def test_versions_section_missing_file_is_graceful(tmp_path):
    gqr = _load()
    html = gqr.versions_section(tmp_path / "nope.yml")
    assert "Software Versions" in html
    assert "not available" in html.lower() or "no " in html.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -v tests/test_generate_qc_report.py -k versions`
Expected: FAIL (`AttributeError: module 'gqr' has no attribute 'parse_versions_yml'`).

- [ ] **Step 3: Write minimal implementation**

Add to `bin/generate_qc_report.py` (after `parse_feature_dist_json`):

```python
def parse_versions_yml(path):
    """
    Minimal two-level YAML parser for a collated versions.yml.

    Structure (concatenated per-process blocks):
        "PROCESS:NAME":
            tool: version
    Returns {process: {tool: version}}. Stdlib-only (no PyYAML) to keep the
    report self-contained. Repeated process keys are merged.
    """
    result = {}
    current = None
    if not path or not Path(path).exists():
        return result
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            if not line.startswith((" ", "\t")):
                # top-level "PROCESS": key
                key = line.strip().rstrip(":").strip().strip('"')
                current = key
                result.setdefault(current, {})
            elif current is not None and ":" in line:
                tool, _, ver = line.strip().partition(":")
                result[current][tool.strip().strip('"')] = ver.strip().strip('"')
    return result


def versions_section(versions_path):
    """Render the collated versions.yml as a per-process software table."""
    versions = parse_versions_yml(versions_path) if versions_path else {}
    if not versions:
        body = '<p class="empty-notice">Version information not available.</p>'
        return section("Software Versions", body)
    tbl = "<table><thead><tr><th>Process</th><th>Tool</th><th>Version</th></tr></thead><tbody>"
    for proc in sorted(versions):
        tools = versions[proc]
        for i, tool in enumerate(sorted(tools)):
            proc_cell = f"<td rowspan='{len(tools)}'>{proc}</td>" if i == 0 else ""
            tbl += f"<tr>{proc_cell}<td>{tool}</td><td>{tools[tool]}</td></tr>"
    tbl += "</tbody></table>"
    return section("Software Versions", tbl)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -v tests/test_generate_qc_report.py -k versions`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git rev-parse HEAD   # re-check before writing
git add bin/generate_qc_report.py tests/test_generate_qc_report.py
git commit -m ":sparkles: Render software versions in QC report (was ignored)"
```

---

### Task A2: run-summary parser + Run Summary / Status Strip / Manifest sections

**Files:**
- Modify: `bin/generate_qc_report.py`
- Test: `tests/test_generate_qc_report.py`

**Interfaces:**
- Produces:
  - `parse_run_summary_json(path) -> dict` (returns `{}` if missing/bad).
  - `run_summary_section(summary: dict) -> str`
  - `status_strip_section(present: dict[str, bool]) -> str` where keys are `"Preprocessing"`, `"Registration"`, `"Segmentation & Quant"`.
  - `manifest_section(summary: dict) -> str`
- Consumes: the JSON shape written by `workflows/mirage.nf` in Task A5:
  ```json
  {"pipeline": {"name": "mirage", "version": "0.1.0"},
   "run": {"timestamp": "...", "mode": "standard", "start": "preprocessing", "stop": "postprocessing"},
   "params": {"registration_method": "valis", "seg_method": "cellsam", "pixel_size": 0.325},
   "manifest": {"totals": {"patients": 1, "images": 3, "channels": 5},
                "patients": {"P001": {"images": 3, "channels": 5}}}}
  ```

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_generate_qc_report.py
import json


def _summary(tmp_path):
    p = tmp_path / "run_summary.json"
    p.write_text(
        json.dumps(
            {
                "pipeline": {"name": "mirage", "version": "0.1.0"},
                "run": {
                    "timestamp": "2026-07-24 10:00:00 UTC",
                    "mode": "standard",
                    "start": "preprocessing",
                    "stop": "postprocessing",
                },
                "params": {
                    "registration_method": "valis",
                    "seg_method": "cellsam",
                    "pixel_size": 0.325,
                },
                "manifest": {
                    "totals": {"patients": 1, "images": 3, "channels": 5},
                    "patients": {"P001": {"images": 3, "channels": 5}},
                },
            }
        )
    )
    return p


def test_parse_run_summary_missing_returns_empty(tmp_path):
    gqr = _load()
    assert gqr.parse_run_summary_json(tmp_path / "nope.json") == {}


def test_run_summary_section(tmp_path):
    gqr = _load()
    s = gqr.parse_run_summary_json(_summary(tmp_path))
    html = gqr.run_summary_section(s)
    assert "mirage" in html and "0.1.0" in html
    assert "valis" in html and "cellsam" in html
    assert "standard" in html


def test_status_strip(tmp_path):
    gqr = _load()
    html = gqr.status_strip_section(
        {"Preprocessing": True, "Registration": True, "Segmentation & Quant": False}
    )
    assert "Preprocessing" in html and "Registration" in html
    assert "Segmentation" in html


def test_manifest_section(tmp_path):
    gqr = _load()
    s = gqr.parse_run_summary_json(_summary(tmp_path))
    html = gqr.manifest_section(s)
    assert "P001" in html
    assert ">3<" in html or "3" in html  # image count present
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -v tests/test_generate_qc_report.py -k "run_summary or status or manifest"`
Expected: FAIL (`parse_run_summary_json` undefined).

- [ ] **Step 3: Write minimal implementation**

Add to `bin/generate_qc_report.py`:

```python
def parse_run_summary_json(path):
    """Load the workflow-written run_summary.json; return {} on any problem."""
    if not path or not Path(path).exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _kv_table(pairs):
    """Render a list of (label, value) tuples as a two-column table."""
    rows = "".join(
        f"<tr><th style='width:240px'>{k}</th><td>{'' if v is None else v}</td></tr>"
        for k, v in pairs
    )
    return f"<table><tbody>{rows}</tbody></table>"


def run_summary_section(summary):
    """Top overview card: pipeline, run context, and key parameters used."""
    if not summary:
        return section(
            "Run Summary", '<p class="empty-notice">Run summary not available.</p>'
        )
    pipe = summary.get("pipeline", {})
    run = summary.get("run", {})
    params = summary.get("params", {})
    pairs = [
        ("Pipeline", f"{pipe.get('name', 'mirage')} v{pipe.get('version', '?')}"),
        ("Run timestamp", run.get("timestamp")),
        ("Mode", run.get("mode")),
        ("Steps", f"{run.get('start', '?')} → {run.get('stop', '?')}"),
    ]
    for key in sorted(params):
        pairs.append((f"param: {key}", params[key]))
    return section("Run Summary", _kv_table(pairs))


def status_strip_section(present):
    """A row of stage badges: ran (green) vs no artifacts (grey)."""
    badges = []
    for stage, ran in present.items():
        color = "#27ae60" if ran else "#95a5a6"
        label = "ran" if ran else "no artifacts"
        badges.append(
            f"<span style='display:inline-block;margin:4px 8px 4px 0;padding:6px 12px;"
            f"border-radius:14px;background:{color};color:#fff;font-size:0.85rem;'>"
            f"{stage}: {label}</span>"
        )
    return section("Pipeline Stages", "<div>" + "".join(badges) + "</div>")


def manifest_section(summary):
    """Sample manifest: totals plus a per-patient image/channel table."""
    manifest = (summary or {}).get("manifest", {})
    if not manifest:
        return section(
            "Sample Manifest", '<p class="empty-notice">Manifest not available.</p>'
        )
    totals = manifest.get("totals", {})
    patients = manifest.get("patients", {})
    head = _kv_table(
        [
            ("Patients", totals.get("patients")),
            ("Images", totals.get("images")),
            ("Channels", totals.get("channels")),
        ]
    )
    tbl = (
        "<table style='margin-top:14px'><thead><tr>"
        "<th>Patient</th><th>Images</th><th>Channels</th>"
        "</tr></thead><tbody>"
    )
    for pid in sorted(patients):
        row = patients[pid]
        tbl += (
            f"<tr><td>{pid}</td><td>{row.get('images', '')}</td>"
            f"<td>{row.get('channels', '')}</td></tr>"
        )
    tbl += "</tbody></table>"
    return section("Sample Manifest", head + tbl)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -v tests/test_generate_qc_report.py -k "run_summary or status or manifest"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git rev-parse HEAD
git add bin/generate_qc_report.py tests/test_generate_qc_report.py
git commit -m ":sparkles: Add run-summary, status-strip, and manifest sections to QC report"
```

---

### Task A3: warp-seg QC table + distance histograms + dedicated seg-overlay section

**Files:**
- Modify: `bin/generate_qc_report.py`
- Test: `tests/test_generate_qc_report.py`

**Interfaces:**
- Produces:
  - `parse_seg_qc_json(path) -> list[tuple[str, str]]` (flattened `key.path -> value` rows; schema-agnostic).
  - `seg_qc_section(seg_qc_dir) -> str`
  - `seg_overlay_section(postprocess_dir) -> str` (renders only `*_seg_overlay*.png`).
  - Modifies `registration_qc_section(reg_dir, feat_dir, valis_dir, dist_plots_dir=None, seg_qc_dir=None)` to append a distance-histogram grid + a warp-seg table.
  - Modifies `postprocess_qc_section` to keep excluding `_seg_overlay` (unchanged) — overlays now render via `seg_overlay_section`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_generate_qc_report.py


def test_parse_seg_qc_json_flattens(tmp_path):
    gqr = _load()
    p = tmp_path / "P001_seg_qc.json"
    p.write_text(json.dumps({"id": "P001", "metrics": {"iou": 0.9, "n": 12}}))
    rows = gqr.parse_seg_qc_json(p)
    d = dict(rows)
    assert d["id"] == "P001"
    assert d["metrics.iou"] == "0.9"
    assert d["metrics.n"] == "12"


def test_seg_qc_section_missing(tmp_path):
    gqr = _load()
    d = tmp_path / "seg_qc"
    d.mkdir()
    html = gqr.seg_qc_section(d)
    assert "Warp" in html or "Segmentation Warp" in html
    assert "not" in html.lower() or "no " in html.lower()


def test_seg_overlay_section_only_overlays(tmp_path):
    gqr = _load()
    d = tmp_path / "postprocess_qc"
    d.mkdir()
    # 1x1 transparent PNG
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6360000002000154a24f9f0000000049454e44ae426082"
    )
    (d / "P001_seg_overlay.png").write_bytes(png)
    (d / "P001_intensity_distributions.png").write_bytes(png)
    html = gqr.seg_overlay_section(d)
    assert "seg_overlay" in html
    assert "intensity_distributions" not in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -v tests/test_generate_qc_report.py -k "seg_qc or overlay"`
Expected: FAIL (`parse_seg_qc_json` undefined).

- [ ] **Step 3: Write minimal implementation**

Add to `bin/generate_qc_report.py`:

```python
def _flatten(prefix, obj, out):
    """Recursively flatten a nested dict into (dotted-key, str-value) rows."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(f"{prefix}.{k}" if prefix else str(k), v, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _flatten(f"{prefix}[{i}]", v, out)
    else:
        out.append((prefix, str(obj)))


def parse_seg_qc_json(path):
    """Flatten a warp-seg QC JSON into (key, value) rows; schema-agnostic."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    _flatten("", data, out)
    return out


def seg_qc_section(seg_qc_dir):
    """Render warp-seg QC JSONs (one table per file)."""
    jsons = list_files(seg_qc_dir, "*.json")
    if not jsons:
        body = '<p class="empty-notice">No warp-segmentation QC metrics found.</p>'
        return section("Segmentation Warp QC", body)
    parts = []
    for jp in jsons:
        parts.append(
            f"<p style='font-size:0.85rem;color:#666;margin:10px 0 6px;'>{Path(jp).name}</p>"
        )
        try:
            rows = parse_seg_qc_json(jp)
        except Exception as exc:  # noqa: BLE001 - report, never crash
            parts.append(
                f'<p class="empty-notice">Could not parse {Path(jp).name}: {exc}</p>'
            )
            continue
        tbl = "<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>"
        for k, v in rows:
            tbl += f"<tr><td>{k}</td><td>{v}</td></tr>"
        tbl += "</tbody></table>"
        parts.append(tbl)
    return section("Segmentation Warp QC", "\n".join(parts))


def seg_overlay_section(postprocess_dir):
    """Dedicated section for the *_seg_overlay* PNGs (the most-inspected QC)."""
    all_pngs = list_files(postprocess_dir, "*.png")
    overlays = [p for p in all_pngs if "_seg_overlay" in p.name]
    return section("Segmentation Overlays", img_grid(overlays, wide=True))
```

Then modify `registration_qc_section` — change its signature and append the two new blocks before `return`:

```python
def registration_qc_section(reg_dir, feat_dir, valis_dir, dist_plots_dir=None, seg_qc_dir=None):
```

Immediately before the final `return section("Registration QC", "\n".join(parts))`, insert:

```python
    # Distance-distribution histograms (previously dropped)
    dist_pngs = list_files(dist_plots_dir, "*.png") if dist_plots_dir else []
    if dist_pngs:
        parts.append(
            "<h3 style='margin:20px 0 8px;font-size:1rem;color:#444;'>Feature-Distance Histograms</h3>"
        )
        parts.append(img_grid(dist_pngs, wide=True))

    # Warp-segmentation QC metrics (previously dropped)
    seg_qc_jsons = list_files(seg_qc_dir, "*.json") if seg_qc_dir else []
    if seg_qc_jsons:
        parts.append(
            "<h3 style='margin:20px 0 8px;font-size:1rem;color:#444;'>Warp-Segmentation QC</h3>"
        )
        for jp in seg_qc_jsons:
            parts.append(
                f"<p style='font-size:0.85rem;color:#666;margin:8px 0 4px;'>{Path(jp).name}</p>"
            )
            try:
                rows = parse_seg_qc_json(jp)
            except Exception as exc:  # noqa: BLE001
                parts.append(f'<p class="empty-notice">Parse error: {exc}</p>')
                continue
            tbl = "<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>"
            for k, v in rows:
                tbl += f"<tr><td>{k}</td><td>{v}</td></tr>"
            tbl += "</tbody></table>"
            parts.append(tbl)
```

(The standalone `seg_qc_section` is retained as an alternative renderer but the wired path uses `registration_qc_section`'s appended block; keep both — `seg_qc_section` is covered by its own test.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -v tests/test_generate_qc_report.py -k "seg_qc or overlay"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git rev-parse HEAD
git add bin/generate_qc_report.py tests/test_generate_qc_report.py
git commit -m ":sparkles: Surface distance histograms, warp-seg QC, and seg overlays in QC report"
```

---

### Task A4: Wire new CLI args into `generate_qc_report.py` main()

**Files:**
- Modify: `bin/generate_qc_report.py` (`parse_args`, `main`, `copy_data`)
- Test: `tests/test_generate_qc_report.py` (end-to-end CLI smoke)

**Interfaces:**
- Consumes: all parsers/sections from A1–A3.
- Produces: CLI flags `--run-summary`, `--distance-plots`, `--seg-qc`; report ordering: Run Summary → Stage strip → Manifest → Preprocessing → Registration (+hist+warp) → Segmentation Overlays → Postprocessing → Segmentation Quality (CSE) → Software Versions.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_generate_qc_report.py


def test_end_to_end_cli_smoke(tmp_path):
    # Minimal inputs: run summary + versions only; everything else empty dirs.
    (tmp_path / "preprocess_qc").mkdir()
    (tmp_path / "registration_qc").mkdir()
    (tmp_path / "feature_dist").mkdir()
    (tmp_path / "valis_summary").mkdir()
    (tmp_path / "postprocess_qc").mkdir()
    (tmp_path / "seg_eval").mkdir()
    (tmp_path / "distance_plots").mkdir()
    (tmp_path / "seg_qc").mkdir()
    rs = _summary(tmp_path)
    v = tmp_path / "v.yml"
    v.write_text('"A:B":\n    tool: 1.0\n')
    out = tmp_path / "report.html"
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--preprocess-qc",
            str(tmp_path / "preprocess_qc"),
            "--registration-qc",
            str(tmp_path / "registration_qc"),
            "--feature-distances",
            str(tmp_path / "feature_dist"),
            "--valis-summary",
            str(tmp_path / "valis_summary"),
            "--postprocess-qc",
            str(tmp_path / "postprocess_qc"),
            "--seg-eval",
            str(tmp_path / "seg_eval"),
            "--distance-plots",
            str(tmp_path / "distance_plots"),
            "--seg-qc",
            str(tmp_path / "seg_qc"),
            "--run-summary",
            str(rs),
            "--versions",
            str(v),
            "--output",
            str(out),
            "--data-dir",
            str(tmp_path / "data"),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    html = out.read_text()
    for header in [
        "Run Summary",
        "Pipeline Stages",
        "Sample Manifest",
        "Preprocessing QC",
        "Registration QC",
        "Segmentation Overlays",
        "Postprocessing QC",
        "Segmentation Quality (CSE)",
        "Software Versions",
    ]:
        assert header in html, f"missing section: {header}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -v tests/test_generate_qc_report.py -k end_to_end`
Expected: FAIL (unrecognized args `--run-summary` / missing sections).

- [ ] **Step 3: Write minimal implementation**

In `parse_args`, add:

```python
p.add_argument("--run-summary", default=None, help="Path to run_summary.json")
p.add_argument(
    "--distance-plots",
    default="distance_plots/",
    help="Directory of registration distance-histogram PNGs",
)
p.add_argument(
    "--seg-qc", default="seg_qc/", help="Directory of warp-segmentation QC JSONs"
)
```

Rewrite `main`'s assembly block:

```python
    summary = parse_run_summary_json(args.run_summary)
    present = {
        "Preprocessing": bool(list_files(args.preprocess_qc, "*.png")),
        "Registration": bool(list_files(args.registration_qc, "*")),
        "Segmentation & Quant": bool(list_files(args.postprocess_qc, "*.png"))
        or bool(list_files(args.seg_eval, "*.csv")),
    }

    html_parts = [html_header(timestamp)]
    html_parts.append(run_summary_section(summary))
    html_parts.append(status_strip_section(present))
    html_parts.append(manifest_section(summary))
    html_parts.append(preprocess_qc_section(args.preprocess_qc))
    html_parts.append(
        registration_qc_section(
            args.registration_qc,
            args.feature_distances,
            args.valis_summary,
            args.distance_plots,
            args.seg_qc,
        )
    )
    html_parts.append(seg_overlay_section(args.postprocess_qc))
    html_parts.append(postprocess_qc_section(args.postprocess_qc))
    html_parts.append(seg_eval_section(args.seg_eval))
    html_parts.append(versions_section(args.versions))
    html_parts.append(html_footer())
```

In `copy_data`, add after the existing `copy_glob` calls:

```python
    copy_glob(args.distance_plots, "*.png", "distance_plots")
    copy_glob(args.seg_qc, "*.json", "seg_qc")
    if args.run_summary and Path(args.run_summary).exists():
        shutil.copy2(args.run_summary, data_dir / "run_summary.json")
    if args.versions and Path(args.versions).exists():
        shutil.copy2(args.versions, data_dir / "collated_versions.yml")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -v tests/test_generate_qc_report.py`
Expected: PASS (all Part A tests).

- [ ] **Step 5: Commit**

```bash
git rev-parse HEAD
git add bin/generate_qc_report.py tests/test_generate_qc_report.py
git commit -m ":sparkles: Wire new QC sections into report CLI and data archive"
```

---

### Task A5: Nextflow wiring — emit, run_summary.json, module inputs, nf-test

**Files:**
- Modify: `subworkflows/local/registration.nf` (add `distance_plots` emit)
- Modify: `modules/local/generate_qc_report.nf` (3 new inputs + flags + stub)
- Modify: `workflows/mirage.nf` (build run_summary.json; forward new channels)
- Modify: `tests/modules/generate_qc_report.nf.test` (new positional inputs)

**Interfaces:**
- Consumes: `ESTIMATE_FEATURE_DISTANCES.out.distance_plots` (`tuple meta, *_distance_histogram.png`), `REGISTRATION.out.seg_qc` (`tuple meta, *_seg_qc.json`).
- Produces: `REGISTRATION.out.distance_plots`; `GENERATE_QC_REPORT` inputs `[7]=run_summary_json`, `[8]=distance_plots (stageAs distance_plots/*)`, `[9]=seg_qc (stageAs seg_qc/*)`.

- [ ] **Step 1: Add `distance_plots` emit in `subworkflows/local/registration.nf`**

Near line 274 where `ch_error_metrics = Channel.empty()` is initialized, add a sibling:

```groovy
    ch_distance_plots = Channel.empty()
```

Where `ESTIMATE_FEATURE_DISTANCES` runs (line ~309–310), add after the existing `.mix(...distance_metrics)`:

```groovy
        ch_distance_plots = ch_distance_plots.mix(ESTIMATE_FEATURE_DISTANCES.out.distance_plots)
```

In the `emit:` block, add:

```groovy
    distance_plots   = ch_distance_plots
```

- [ ] **Step 2: Add three inputs to `modules/local/generate_qc_report.nf`**

Replace the `input:` block with (append the three new lines after `versions_yml`):

```groovy
    input:
    path(preprocess_qc_pngs, stageAs: 'preprocess_qc/*')
    path(registration_qc_pngs, stageAs: 'registration_qc/*')
    path(feature_distance_jsons, stageAs: 'feature_dist/*')
    path(valis_summary_csvs, stageAs: 'valis_summary/*')
    path(postprocess_qc_pngs, stageAs: 'postprocess_qc/*')
    path(seg_eval_csvs, stageAs: 'seg_eval/*')
    path(versions_yml)
    path(run_summary_json)
    path(distance_plot_pngs, stageAs: 'distance_plots/*')
    path(seg_qc_jsons, stageAs: 'seg_qc/*')
```

In the `script:` block, add the three flags to the `generate_qc_report.py` invocation (after `--versions`):

```groovy
        --run-summary ${run_summary_json} \\
        --distance-plots distance_plots/ \\
        --seg-qc seg_qc/ \\
```

(The `stub:` block needs no change — it only touches the output HTML.)

- [ ] **Step 3: Build run_summary.json and forward channels in `workflows/mirage.nf`**

At the top of the file, ensure the JSON helper is imported:

```groovy
import groovy.json.JsonOutput
```

Inside the `if (!params.skip_final_qc_report)` block, before the `GENERATE_QC_REPORT(...)` call, add new channels next to the existing ones:

```groovy
        def ch_distance_plots = Channel.empty()
        def ch_seg_qc_jsons   = Channel.empty()
```

In the registration `if` branch (where `ch_registration_qc_pngs` etc. are mixed), add:

```groovy
            ch_distance_plots = ch_distance_plots
                .mix(REGISTRATION.out.distance_plots.map { meta, files -> files })
            ch_seg_qc_jsons = ch_seg_qc_jsons
                .mix(REGISTRATION.out.seg_qc.map { meta, files -> files })
```

Build the run-summary file (uses the already-computed `patient_counts` /
`channel_counts` maps and `params`/`workflow`):

```groovy
        def manifest_patients = patient_counts.collectEntries { pid, imgs ->
            [(pid): [images: imgs, channels: (channel_counts[pid] ?: 0)]]
        }
        def run_summary_map = [
            pipeline: [name: workflow.manifest.name, version: workflow.manifest.version],
            run: [
                timestamp: new java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss 'UTC'")
                    .format(new Date()),
                mode: (params.mode ?: 'standard'),
                start: params.start,
                stop: effective_stop,
            ],
            params: [
                registration_method: params.registration_method,
                seg_method: params.seg_method,
                quantify_compartments: params.quantify_compartments,
                expanded_quantification: params.expanded_quantification,
                pixel_size: params.pixel_size,
                cse_pixel_size_um: params.cse_pixel_size_um,
            ],
            manifest: [
                totals: [
                    patients: patient_counts.size(),
                    images: (patient_counts.values().sum() ?: 0),
                    channels: (channel_counts.values().sum() ?: 0),
                ],
                patients: manifest_patients,
            ],
        ]
        def ch_run_summary = Channel
            .of(JsonOutput.prettyPrint(JsonOutput.toJson(run_summary_map)))
            .collectFile(name: 'run_summary.json')
```

Update the `GENERATE_QC_REPORT(...)` call to pass the three new inputs (in this order — matching the module):

```groovy
        GENERATE_QC_REPORT(
            ch_preprocess_qc_pngs.collect().ifEmpty([]),
            ch_registration_qc_pngs.collect().ifEmpty([]),
            ch_feature_dist_jsons.collect().ifEmpty([]),
            ch_valis_summary_csvs.collect().ifEmpty([]),
            ch_postprocess_qc_pngs.collect().ifEmpty([]),
            ch_seg_eval_csv.collect().ifEmpty([]),
            ch_collated_versions,
            ch_run_summary,
            ch_distance_plots.collect().ifEmpty([]),
            ch_seg_qc_jsons.collect().ifEmpty([]),
        )
```

- [ ] **Step 4: Rebuild the nf-test stub inputs (full block)**

IMPORTANT (discovered at execution): the existing test is **already broken on main** —
it specifies only 6 inputs for the 7-input process (`declares 7 input channels but
6 were specified`) and the entries are misaligned (versions.yml landed in the
`seg_eval` slot, `postprocess_qc` has no entry). Do **not** append; **replace the
entire `process { """ ... """ }` input block** with the correct 10-input block
(0–9), matching the module's new input order exactly. The stub ignores file
content, so reuse existing testdata files as stand-ins:

```groovy
                input[0] = [ file('${projectDir}/tests/testdata/P001_ref.ome.tiff', checkIfExists: true) ]
                input[1] = [ file('${projectDir}/tests/testdata/P001_ref.ome.tiff', checkIfExists: true) ]
                input[2] = [ file('${projectDir}/tests/testdata/sample_feature_distances.json', checkIfExists: true) ]
                input[3] = [ file('${projectDir}/tests/testdata/sample_valis_summary.csv', checkIfExists: true) ]
                input[4] = [ file('${projectDir}/tests/testdata/P001_ref.ome.tiff', checkIfExists: true) ]
                input[5] = [ file('${projectDir}/tests/testdata/sample_morphology.csv', checkIfExists: true) ]
                input[6] = file('${projectDir}/tests/testdata/sample_versions.yml', checkIfExists: true)
                input[7] = file('${projectDir}/tests/testdata/sample_versions.yml', checkIfExists: true)
                input[8] = [ file('${projectDir}/tests/testdata/P001_ref.ome.tiff', checkIfExists: true) ]
                input[9] = [ file('${projectDir}/tests/testdata/sample_feature_distances.json', checkIfExists: true) ]
```

Mapping: 0=preprocess_qc, 1=registration_qc, 2=feature_dist, 3=valis_summary,
4=postprocess_qc, 5=seg_eval, 6=versions, 7=run_summary, 8=distance_plots,
9=seg_qc. This step fixes the pre-existing failure **and** adds the new inputs.
Verify the referenced testdata files exist (they are produced by
`tests/testdata/generate_complete_testdata.py`); if `sample_morphology.csv` or
`sample_valis_summary.csv` are absent, generate testdata first.

- [ ] **Step 5: Verify the pipeline compiles and stub-runs**

Run:
```bash
nextflow run . -profile test,docker -stub --outdir results_stub
```
Expected: completes; `results_stub/qc/mirage_qc_report_*.html` exists.

Run:
```bash
nf-test test tests/modules/generate_qc_report.nf.test --profile test,docker
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git rev-parse HEAD
git add subworkflows/local/registration.nf modules/local/generate_qc_report.nf \
        workflows/mirage.nf tests/modules/generate_qc_report.nf.test
git commit -m ":sparkles: Wire run-summary, distance plots, and warp-seg QC into GENERATE_QC_REPORT"
```

---

# PART B — Computational-Resources Report

Files touched in Part B:
- Create: `bin/generate_resource_report.py` (tracked `100755`).
- Test: `tests/test_generate_resource_report.py`.
- Modify: `main.nf` — `workflow.onComplete` best-effort hook.
- Modify: `docs/parameters.md` — document the resource report.

All Part B Python tests run with:
`pytest -v tests/test_generate_resource_report.py`

---

### Task B1: trace value normalizers + trace.txt parser

**Files:**
- Create: `bin/generate_resource_report.py`
- Test: `tests/test_generate_resource_report.py`

**Interfaces:**
- Produces:
  - `parse_bytes(s) -> float | None` (`"3.2 GB"` → `3435973836.8`, `"-"`/`""` → None).
  - `parse_duration(s) -> float | None` seconds (`"12m 4s"` → `724.0`, `"1.5s"` → `1.5`, `"2h 1m"` → `7260.0`).
  - `parse_percent(s) -> float | None` (`"142.3%"` → `142.3`).
  - `parse_trace(path) -> list[dict]` (one dict per task with normalized numeric fields + raw `process`, `tag`, `exit`, `status`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_generate_resource_report.py
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "generate_resource_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("grr", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_bytes():
    grr = _load()
    assert grr.parse_bytes("3.2 GB") == round(3.2 * 1024**3, 1)
    assert grr.parse_bytes("512 MB") == 512 * 1024**2
    assert grr.parse_bytes("-") is None
    assert grr.parse_bytes("") is None


def test_parse_duration():
    grr = _load()
    assert grr.parse_duration("1.5s") == 1.5
    assert grr.parse_duration("12m 4s") == 724.0
    assert grr.parse_duration("2h 1m") == 7260.0
    assert grr.parse_duration("-") is None


def test_parse_percent():
    grr = _load()
    assert grr.parse_percent("142.3%") == 142.3
    assert grr.parse_percent("-") is None


def test_parse_trace(tmp_path):
    grr = _load()
    t = tmp_path / "trace.txt"
    t.write_text(
        "task_id\tprocess\ttag\tname\tstatus\texit\tsubmit\tstart\tcomplete\t"
        "duration\trealtime\t%cpu\tcpus\tmemory\tpeak_rss\tpeak_vmem\trchar\twchar\n"
        "1\tMIRAGE:PRE:CONVERT_IMAGE\tP001\tname\tCOMPLETED\t0\t-\t-\t-\t"
        "12m 4s\t10m\t142.3%\t8\t8 GB\t3.2 GB\t4 GB\t1 GB\t500 MB\n"
    )
    rows = grr.parse_trace(t)
    assert len(rows) == 1
    r = rows[0]
    assert r["process"] == "MIRAGE:PRE:CONVERT_IMAGE"
    assert r["tag"] == "P001"
    assert r["realtime_s"] == 600.0
    assert r["peak_rss_b"] == round(3.2 * 1024**3, 1)
    assert r["cpu_pct"] == 142.3
    assert r["exit"] == "0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -v tests/test_generate_resource_report.py -k "parse"`
Expected: FAIL (module file does not exist yet).

- [ ] **Step 3: Write minimal implementation**

Create `bin/generate_resource_report.py`:

```python
#!/usr/bin/env python3
"""
generate_resource_report.py
Self-contained HTML computational-resources report for a MIRAGE run.
Joins the aggregated input-size log with Nextflow's trace.txt. Stdlib only.
"""

import argparse
import csv
import re
from datetime import datetime
from pathlib import Path

_UNIT = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def parse_bytes(s):
    """Parse a Nextflow byte string ('3.2 GB', '512 MB', '-') to float bytes."""
    if s is None:
        return None
    s = s.strip()
    if not s or s in {"-", "0"} and s == "-":
        return None
    m = re.match(r"^([\d.]+)\s*([KMGT]?B)$", s, re.IGNORECASE)
    if m:
        return round(float(m.group(1)) * _UNIT[m.group(2).upper()], 1)
    try:
        return float(s)
    except ValueError:
        return None


def parse_duration(s):
    """Parse a duration ('12m 4s', '2h 1m', '1.5s', '-') to float seconds."""
    if s is None:
        return None
    s = s.strip()
    if not s or s == "-":
        return None
    total = 0.0
    found = False
    for value, unit in re.findall(r"([\d.]+)\s*(ms|s|m|h|d)", s):
        found = True
        v = float(value)
        total += {"ms": v / 1000, "s": v, "m": v * 60, "h": v * 3600, "d": v * 86400}[
            unit
        ]
    if found:
        return total
    try:
        return float(s)
    except ValueError:
        return None


def parse_percent(s):
    """Parse a percentage string ('142.3%', '-') to float."""
    if s is None:
        return None
    s = s.strip().rstrip("%")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_trace(path):
    """Parse trace.txt (TSV) into a list of per-task dicts with normalized fields."""
    rows = []
    if not path or not Path(path).exists():
        return rows
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for r in reader:
            rows.append(
                {
                    "process": (r.get("process") or "").strip(),
                    "tag": (r.get("tag") or "").strip(),
                    "name": (r.get("name") or "").strip(),
                    "status": (r.get("status") or "").strip(),
                    "exit": (r.get("exit") or "").strip(),
                    "realtime_s": parse_duration(r.get("realtime")),
                    "duration_s": parse_duration(r.get("duration")),
                    "cpu_pct": parse_percent(r.get("%cpu")),
                    "peak_rss_b": parse_bytes(r.get("peak_rss")),
                    "peak_vmem_b": parse_bytes(r.get("peak_vmem")),
                    "rchar_b": parse_bytes(r.get("rchar")),
                    "wchar_b": parse_bytes(r.get("wchar")),
                }
            )
    return rows


if __name__ == "__main__":
    raise SystemExit(main())  # noqa: F821 - main added in Task B3
```

Note: the trailing `__main__` guard references `main`, added in Task B3. To keep
B1 runnable in isolation, temporarily end the file at the `parse_trace` function
and add the `__main__` guard in B3. (When executing sequentially, simply omit the
guard here and add it in B3.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -v tests/test_generate_resource_report.py -k "parse"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git rev-parse HEAD
git add bin/generate_resource_report.py tests/test_generate_resource_report.py
git commit -m ":sparkles: Add trace.txt parser and unit normalizers for resource report"
```

---

### Task B2: size-log parser + per-process rollup + size↔trace join

**Files:**
- Modify: `bin/generate_resource_report.py`
- Test: `tests/test_generate_resource_report.py`

**Interfaces:**
- Produces:
  - `parse_size_log(path) -> dict[(process, sample_id), int]` (summed bytes per key).
  - `rollup_by_process(trace_rows) -> list[dict]` (per-process aggregates: `n_tasks`, `realtime_total_s`, `realtime_mean_s`, `cpu_max_pct`, `peak_rss_max_b`, `peak_vmem_max_b`, `rchar_total_b`, `wchar_total_b`, `n_failed`).
  - `join_size(trace_rows, size_map) -> list[dict]` (each trace row + `input_bytes` looked up by `(process, tag)` with fallback to any sample under the same process).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_generate_resource_report.py


def test_parse_size_log(tmp_path):
    grr = _load()
    p = tmp_path / "input_sizes.csv"
    p.write_text(
        "process,sample_id,filename,bytes\n"
        "MIRAGE:PRE:CONVERT_IMAGE,P001,a.tiff,100\n"
        "MIRAGE:PRE:CONVERT_IMAGE,P001,b.tiff,50\n"
        "STUB,P001,stub,0\n"
    )
    m = grr.parse_size_log(p)
    assert m[("MIRAGE:PRE:CONVERT_IMAGE", "P001")] == 150


def test_rollup_by_process():
    grr = _load()
    rows = [
        {
            "process": "A",
            "tag": "P1",
            "exit": "0",
            "realtime_s": 10.0,
            "cpu_pct": 100.0,
            "peak_rss_b": 200.0,
            "peak_vmem_b": 300.0,
            "rchar_b": 5.0,
            "wchar_b": 2.0,
        },
        {
            "process": "A",
            "tag": "P2",
            "exit": "0",
            "realtime_s": 30.0,
            "cpu_pct": 150.0,
            "peak_rss_b": 400.0,
            "peak_vmem_b": 500.0,
            "rchar_b": 7.0,
            "wchar_b": 1.0,
        },
    ]
    roll = {r["process"]: r for r in grr.rollup_by_process(rows)}
    a = roll["A"]
    assert a["n_tasks"] == 2
    assert a["realtime_total_s"] == 40.0
    assert a["realtime_mean_s"] == 20.0
    assert a["peak_rss_max_b"] == 400.0
    assert a["cpu_max_pct"] == 150.0


def test_join_size_exact_and_fallback():
    grr = _load()
    trace = [
        {"process": "A", "tag": "P001", "realtime_s": 1.0, "peak_rss_b": 10.0},
        {"process": "A", "tag": "P001_slideX", "realtime_s": 2.0, "peak_rss_b": 20.0},
    ]
    size = {("A", "P001"): 999}
    joined = grr.join_size(trace, size)
    assert joined[0]["input_bytes"] == 999  # exact (process, tag)
    assert joined[1]["input_bytes"] == 999  # fallback: same process, sample prefix
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -v tests/test_generate_resource_report.py -k "size or rollup or join"`
Expected: FAIL (`parse_size_log` undefined).

- [ ] **Step 3: Write minimal implementation**

Add to `bin/generate_resource_report.py` (before the `__main__` guard):

```python
def parse_size_log(path):
    """Sum input bytes per (process, sample_id) from input_sizes.csv."""
    out = {}
    if not path or not Path(path).exists():
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            proc = (row.get("process") or "").strip()
            sample = (row.get("sample_id") or "").strip()
            if not proc or proc == "STUB":
                continue
            try:
                b = int(float(row.get("bytes") or 0))
            except ValueError:
                b = 0
            out[(proc, sample)] = out.get((proc, sample), 0) + b
    return out


def _maxf(values):
    """Max of the non-None floats, or None."""
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def _sumf(values):
    """Sum of the non-None floats (0.0 if all None)."""
    return sum(v for v in values if v is not None)


def rollup_by_process(trace_rows):
    """Aggregate trace rows into per-process resource summaries."""
    groups = {}
    for r in trace_rows:
        groups.setdefault(r["process"], []).append(r)
    out = []
    for proc, rows in sorted(groups.items()):
        rts = [r.get("realtime_s") for r in rows]
        realtime_total = _sumf(rts)
        n = len(rows)
        out.append(
            {
                "process": proc,
                "n_tasks": n,
                "realtime_total_s": realtime_total,
                "realtime_mean_s": round(realtime_total / n, 1) if n else 0.0,
                "cpu_max_pct": _maxf([r.get("cpu_pct") for r in rows]),
                "peak_rss_max_b": _maxf([r.get("peak_rss_b") for r in rows]),
                "peak_vmem_max_b": _maxf([r.get("peak_vmem_b") for r in rows]),
                "rchar_total_b": _sumf([r.get("rchar_b") for r in rows]),
                "wchar_total_b": _sumf([r.get("wchar_b") for r in rows]),
                "n_failed": sum(
                    1 for r in rows if r.get("exit") not in ("0", "", None)
                ),
            }
        )
    return out


def join_size(trace_rows, size_map):
    """Attach input_bytes to each trace row via (process, tag), with prefix fallback."""
    # Pre-index sample ids by process for fallback matching.
    by_proc = {}
    for (proc, sample), b in size_map.items():
        by_proc.setdefault(proc, []).append((sample, b))
    joined = []
    for r in trace_rows:
        proc, tag = r["process"], r.get("tag", "")
        input_bytes = size_map.get((proc, tag))
        if input_bytes is None:
            # Fallback: a size sample whose id is a prefix of the trace tag
            # (trace tag is meta.id = "<patient>_<stem>"; size sample is patient).
            for sample, b in by_proc.get(proc, []):
                if sample and (tag == sample or tag.startswith(sample)):
                    input_bytes = b
                    break
        joined.append({**r, "input_bytes": input_bytes})
    return joined
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -v tests/test_generate_resource_report.py -k "size or rollup or join"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git rev-parse HEAD
git add bin/generate_resource_report.py tests/test_generate_resource_report.py
git commit -m ":sparkles: Add size-log parsing, per-process rollup, and size-trace join"
```

---

### Task B3: HTML assembly + CLI entry point

**Files:**
- Modify: `bin/generate_resource_report.py`
- Test: `tests/test_generate_resource_report.py`

**Interfaces:**
- Produces: `fmt_bytes(b) -> str`, `fmt_secs(s) -> str`, `build_html(trace_rows, size_map, timestamp) -> str`, `parse_args()`, `main() -> int`.
- CLI: `--trace .trace/trace.txt --size-log size_logs/input_sizes.csv --output mirage_resource_report.html [--native-report .trace/report.html --native-timeline .trace/timeline.html]`. Missing inputs → still writes an HTML with a notice, exit 0.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_generate_resource_report.py
import subprocess
import sys


def test_cli_writes_report(tmp_path):
    trace = tmp_path / "trace.txt"
    trace.write_text(
        "task_id\tprocess\ttag\tname\tstatus\texit\tsubmit\tstart\tcomplete\t"
        "duration\trealtime\t%cpu\tcpus\tmemory\tpeak_rss\tpeak_vmem\trchar\twchar\n"
        "1\tMIRAGE:PRE:CONVERT_IMAGE\tP001\tn\tCOMPLETED\t0\t-\t-\t-\t"
        "12m\t10m\t142%\t8\t8 GB\t3.2 GB\t4 GB\t1 GB\t500 MB\n"
        "2\tMIRAGE:REG:REGISTER\tP001\tn\tFAILED\t1\t-\t-\t-\t"
        "1h\t1h\t90%\t8\t8 GB\t7 GB\t9 GB\t2 GB\t1 GB\n"
    )
    size = tmp_path / "input_sizes.csv"
    size.write_text(
        "process,sample_id,filename,bytes\n"
        "MIRAGE:PRE:CONVERT_IMAGE,P001,a.tiff,1073741824\n"
    )
    out = tmp_path / "resource.html"
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--trace",
            str(trace),
            "--size-log",
            str(size),
            "--output",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    html = out.read_text()
    assert "Resource" in html
    assert "MIRAGE:PRE:CONVERT_IMAGE" in html
    assert "MIRAGE:REG:REGISTER" in html
    assert "Retries" in html or "Failures" in html


def test_cli_missing_inputs_is_graceful(tmp_path):
    out = tmp_path / "resource.html"
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--trace",
            str(tmp_path / "nope.txt"),
            "--size-log",
            str(tmp_path / "nope.csv"),
            "--output",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert out.exists()
    assert "not available" in out.read_text().lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -v tests/test_generate_resource_report.py -k cli`
Expected: FAIL (`main` / argparse not present).

- [ ] **Step 3: Write minimal implementation**

Replace the temporary `__main__` guard at the end of `bin/generate_resource_report.py` with the formatting helpers, HTML builder, and CLI:

```python
def fmt_bytes(b):
    if b is None:
        return "N/A"
    b = float(b)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024 or unit == "TB":
            return f"{b:.1f} {unit}"
        b /= 1024


def fmt_secs(s):
    if s is None:
        return "N/A"
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


_CSS = """
body{font-family:'Segoe UI',Arial,sans-serif;background:#f4f6f9;color:#333;margin:0}
header{background:#1a2332;color:#fff;padding:24px 40px}
main{max-width:1400px;margin:32px auto;padding:0 24px 60px}
section{background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.1);margin-bottom:32px;overflow:hidden}
h2{background:#2c3e50;color:#fff;padding:14px 20px;font-size:1.15rem;margin:0}
.body{padding:20px}
table{border-collapse:collapse;width:100%;font-size:.88rem}
th{background:#ecf0f1;text-align:left;padding:8px 12px;border-bottom:2px solid #bdc3c7}
td{padding:7px 12px;border-bottom:1px solid #ecf0f1}
tr:hover td{background:#f8f9fa}
.empty{color:#888;font-style:italic}
.fail{color:#c0392b;font-weight:600}
"""


def _section(title, body):
    return f"<section><h2>{title}</h2><div class='body'>{body}</div></section>"


def build_html(
    trace_rows, size_map, timestamp, native_report=None, native_timeline=None
):
    parts = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>",
        f"<title>MIRAGE Resource Report</title><style>{_CSS}</style></head><body>",
        f"<header><h1>MIRAGE Computational Resource Report</h1>"
        f"<div style='opacity:.75;font-size:.9rem'>Generated: {timestamp}</div></header><main>",
    ]

    if not trace_rows:
        parts.append(
            _section(
                "Resource Usage",
                "<p class='empty'>Trace data not available "
                "(run with --enable_trace to collect it).</p>",
            )
        )
        parts.append("</main></body></html>")
        return "".join(parts)

    # Run totals
    total_wall = _sumf([r.get("realtime_s") for r in trace_rows])
    n_fail = sum(1 for r in trace_rows if r.get("exit") not in ("0", "", None))
    peak = _maxf([r.get("peak_rss_b") for r in trace_rows])
    totals = (
        f"<table><tbody>"
        f"<tr><th>Total tasks</th><td>{len(trace_rows)}</td></tr>"
        f"<tr><th>Total CPU wall-time</th><td>{fmt_secs(total_wall)}</td></tr>"
        f"<tr><th>Failed/non-zero exit</th><td>{n_fail}</td></tr>"
        f"<tr><th>Peak single-task RSS</th><td>{fmt_bytes(peak)}</td></tr>"
        f"</tbody></table>"
    )
    parts.append(_section("Run Totals", totals))

    # Per-process rollup
    roll = rollup_by_process(trace_rows)
    tbl = (
        "<table><thead><tr><th>Process</th><th>Tasks</th><th>Total time</th>"
        "<th>Mean time</th><th>Max %CPU</th><th>Max peak RSS</th>"
        "<th>Max peak VMEM</th><th>Read</th><th>Write</th><th>Failed</th>"
        "</tr></thead><tbody>"
    )
    for r in roll:
        tbl += (
            f"<tr><td>{r['process']}</td><td>{r['n_tasks']}</td>"
            f"<td>{fmt_secs(r['realtime_total_s'])}</td>"
            f"<td>{fmt_secs(r['realtime_mean_s'])}</td>"
            f"<td>{'' if r['cpu_max_pct'] is None else r['cpu_max_pct']}</td>"
            f"<td>{fmt_bytes(r['peak_rss_max_b'])}</td>"
            f"<td>{fmt_bytes(r['peak_vmem_max_b'])}</td>"
            f"<td>{fmt_bytes(r['rchar_total_b'])}</td>"
            f"<td>{fmt_bytes(r['wchar_total_b'])}</td>"
            f"<td class='{'fail' if r['n_failed'] else ''}'>{r['n_failed']}</td></tr>"
        )
    tbl += "</tbody></table>"
    parts.append(_section("Per-Process Resource Rollup", tbl))

    # Resource vs input size
    joined = join_size(trace_rows, size_map)
    with_size = [j for j in joined if j.get("input_bytes")]
    if with_size:
        tbl = (
            "<table><thead><tr><th>Process</th><th>Sample (tag)</th>"
            "<th>Input size</th><th>Peak RSS</th><th>Realtime</th>"
            "<th>RSS / input GB</th></tr></thead><tbody>"
        )
        for j in sorted(with_size, key=lambda x: -(x.get("peak_rss_b") or 0)):
            gb = j["input_bytes"] / 1024**3
            ratio = (
                (j["peak_rss_b"] / j["input_bytes"]) if j.get("peak_rss_b") else None
            )
            tbl += (
                f"<tr><td>{j['process']}</td><td>{j.get('tag', '')}</td>"
                f"<td>{fmt_bytes(j['input_bytes'])}</td>"
                f"<td>{fmt_bytes(j.get('peak_rss_b'))}</td>"
                f"<td>{fmt_secs(j.get('realtime_s'))}</td>"
                f"<td>{'' if ratio is None else f'{ratio:.1f}x'}</td></tr>"
            )
        tbl += "</tbody></table>"
        parts.append(_section("Resource vs Input Size", tbl))
    else:
        parts.append(
            _section(
                "Resource vs Input Size",
                "<p class='empty'>No size logs matched trace tasks.</p>",
            )
        )

    # Top-N heaviest / slowest
    heaviest = sorted(
        [r for r in trace_rows if r.get("peak_rss_b")], key=lambda x: -x["peak_rss_b"]
    )[:10]
    slowest = sorted(
        [r for r in trace_rows if r.get("realtime_s")], key=lambda x: -x["realtime_s"]
    )[:10]

    def _top(rows, valf, fmt):
        t = "<table><thead><tr><th>Process</th><th>Sample</th><th>Value</th></tr></thead><tbody>"
        for r in rows:
            t += f"<tr><td>{r['process']}</td><td>{r.get('tag', '')}</td><td>{fmt(valf(r))}</td></tr>"
        return t + "</tbody></table>"

    parts.append(
        _section(
            "Top 10 by Peak RSS", _top(heaviest, lambda r: r["peak_rss_b"], fmt_bytes)
        )
    )
    parts.append(
        _section(
            "Top 10 by Runtime", _top(slowest, lambda r: r["realtime_s"], fmt_secs)
        )
    )

    # Retries & failures
    fails = [r for r in trace_rows if r.get("exit") not in ("0", "", None)]
    if fails:
        t = "<table><thead><tr><th>Process</th><th>Sample</th><th>Status</th><th>Exit</th></tr></thead><tbody>"
        for r in fails:
            t += (
                f"<tr><td>{r['process']}</td><td>{r.get('tag', '')}</td>"
                f"<td>{r.get('status', '')}</td><td class='fail'>{r.get('exit', '')}</td></tr>"
            )
        t += "</tbody></table>"
        parts.append(_section("Retries &amp; Failures", t))
    else:
        parts.append(
            _section(
                "Retries &amp; Failures",
                "<p class='empty'>No failed or non-zero-exit tasks.</p>",
            )
        )

    # Pointers to native reports
    links = []
    if native_report:
        links.append(
            f"<li>Interactive execution report: <code>{native_report}</code></li>"
        )
    if native_timeline:
        links.append(f"<li>Timeline: <code>{native_timeline}</code></li>")
    if links:
        parts.append(
            _section("Nextflow Native Reports", "<ul>" + "".join(links) + "</ul>")
        )

    parts.append("</main></body></html>")
    return "".join(parts)


def parse_args():
    p = argparse.ArgumentParser(
        description="Generate MIRAGE computational-resource report"
    )
    p.add_argument("--trace", default=".trace/trace.txt")
    p.add_argument("--size-log", default=None)
    p.add_argument("--output", default="mirage_resource_report.html")
    p.add_argument("--native-report", default=None)
    p.add_argument("--native-timeline", default=None)
    return p.parse_args()


def main():
    args = parse_args()
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    trace_rows = parse_trace(args.trace)
    size_map = parse_size_log(args.size_log)
    html = build_html(
        trace_rows, size_map, timestamp, args.native_report, args.native_timeline
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Resource report written to: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -v tests/test_generate_resource_report.py`
Expected: PASS (all Part B tests).

- [ ] **Step 5: Set the executable bit (invoked by name from main.nf)**

Run:
```bash
git update-index --chmod=+x bin/generate_resource_report.py
git ls-files -s bin/generate_resource_report.py   # expect leading 100755
```

- [ ] **Step 6: Commit**

```bash
git rev-parse HEAD
git add bin/generate_resource_report.py tests/test_generate_resource_report.py
git commit -m ":sparkles: Assemble resource-report HTML with per-process, size, and failure views"
```

---

### Task B4: `workflow.onComplete` best-effort hook + docs

**Files:**
- Modify: `main.nf`
- Modify: `docs/parameters.md`

**Interfaces:**
- Consumes: `bin/generate_resource_report.py` (CLI from B3), `params.enable_trace`, `params.trace_dir`, `params.outdir`.
- Produces: `${params.outdir}/qc/mirage_resource_report.html` after a run when tracing is on. Failure of the hook logs a warning and does NOT fail the run.

- [ ] **Step 1: Add the onComplete hook to `main.nf`**

Append to `main.nf` (after the `workflow { MIRAGE() }` block):

```groovy
/*
================================================================================
    POST-RUN: COMPUTATIONAL RESOURCE REPORT (best-effort)
================================================================================
    trace.txt is only finalized at workflow completion, so the resource report
    is generated here rather than as an in-DAG process. Any failure (no python3
    on the head node, missing trace) logs a warning and never fails the run;
    the script is also runnable by hand against an existing outdir + trace dir.
*/
workflow.onComplete {
    if (!params.enable_trace) {
        return
    }
    try {
        def script    = "${projectDir}/bin/generate_resource_report.py"
        def trace_txt = "${params.trace_dir}/trace.txt"
        def size_log  = "${params.outdir}/size_logs/input_sizes.csv"
        def out_html  = "${params.outdir}/qc/mirage_resource_report.html"
        new File("${params.outdir}/qc").mkdirs()
        def cmd = [
            'python3', script,
            '--trace', trace_txt,
            '--size-log', size_log,
            '--output', out_html,
            '--native-report', "${params.trace_dir}/report.html",
            '--native-timeline', "${params.trace_dir}/timeline.html",
        ]
        def proc = cmd.execute()
        proc.waitForProcessOutput(System.out, System.err)
        if (proc.exitValue() == 0) {
            log.info "Resource report: ${out_html}"
        } else {
            log.warn "Resource report generation exited ${proc.exitValue()} (non-fatal)."
        }
    } catch (Exception e) {
        log.warn "Could not generate resource report (non-fatal): ${e.message}"
    }
}
```

- [ ] **Step 2: Verify the hook fires on a stub run**

Run:
```bash
nextflow run . -profile test,docker -stub --outdir results_stub
ls results_stub/qc/mirage_resource_report.html
```
Expected: the HTML exists (built from the stub trace; sections may show "not available" where the stub emitted no data — acceptable).

Note: if the head node has no `python3`, the run still completes green and logs
the warning — verify the warning appears rather than a failure.

- [ ] **Step 3: Document both reports in `docs/parameters.md`**

Under the existing QC/output notes, add:

```markdown
### Reports

- **`qc/mirage_qc_report_<timestamp>.html`** — aggregated QC report: run summary,
  pipeline-stage status, sample manifest, preprocessing / registration (overlays,
  rTRE, feature distances + histograms, warp-seg QC) / segmentation overlays /
  postprocessing QC, CellSegmentationEvaluator metrics, and software versions.
  Controlled by `skip_final_qc_report`.
- **`qc/mirage_resource_report.html`** — computational-resource report built from
  the per-task size logs and Nextflow `trace.txt`: run totals, per-process
  rollup, resource-vs-input-size, top-N heaviest/slowest tasks, and
  retries/failures. Generated at run completion when `enable_trace` is set;
  re-runnable by hand via `bin/generate_resource_report.py`. Complements
  Nextflow's native `report.html` / `timeline.html`.
```

- [ ] **Step 4: Commit**

```bash
git rev-parse HEAD
git add main.nf docs/parameters.md
git commit -m ":sparkles: Generate resource report on completion and document both reports"
```

---

## Final verification (run after all tasks)

- [ ] `pytest -v tests/test_generate_qc_report.py tests/test_generate_resource_report.py` — all PASS.
- [ ] `nextflow run . -profile test,docker -stub --outdir results_stub` — completes; both `results_stub/qc/mirage_qc_report_*.html` and `results_stub/qc/mirage_resource_report.html` exist.
- [ ] `nf-test test tests/modules/generate_qc_report.nf.test --profile test,docker` — PASS.
- [ ] `ruff check bin/generate_qc_report.py bin/generate_resource_report.py` — clean (ruff is the repo linter; advisory).
- [ ] `git ls-files -s bin/generate_resource_report.py` shows `100755`.

## Self-review notes (author)

- **Spec coverage:** every §2 QC section → Tasks A1–A4; wiring §2.2 → A5; every §3 resource section → B1–B4. No spec requirement left unassigned.
- **Type consistency:** `parse_seg_qc_json` returns `list[tuple]` and is consumed identically in `seg_qc_section` and the `registration_qc_section` append block. `rollup_by_process` keys (`realtime_total_s`, `peak_rss_max_b`, …) match their use in `build_html`. Trace dict keys (`realtime_s`, `peak_rss_b`, `cpu_pct`, `exit`, `tag`, `process`) are identical across B1/B2/B3.
- **Graceful-degradation:** every parser guards missing paths; `main()`/`build_html` never raise on empty inputs; the onComplete hook is wrapped in try/catch and gated on `enable_trace`.
