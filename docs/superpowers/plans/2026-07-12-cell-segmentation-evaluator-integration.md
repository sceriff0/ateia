# CellSegmentationEvaluator Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add reference-free segmentation-quality scoring (CellSegmentationEvaluator / CSE) to Mirage as an informational per-patient QC step, with per-patient Nextflow fan-out and a bit-exact vectorized speedup of CSE's inner loops.

**Architecture:** A new `SEG_QUALITY_EVAL` process runs after `SEGMENT` in the `POSTPROCESSING` subworkflow (inheriting patient-level parallelism), fed the cell mask + nucleus mask + reference image joined on `meta.patient_id`. It calls a *vendored, patched* copy of CSE's 2D metric code (`bin/utils/cse/`) whose per-cell/per-channel Python loops are replaced by `scipy.ndimage` labeled reductions and whose independent k=2..10 clustering passes run concurrently — all changes proven equal to upstream within 1e-6 by a golden-snapshot pytest. Per-patient JSONs merge into one CSV that threads into the existing QC report via the `feature_distance_jsons` wiring pattern. The process runs in a new, fully pinned `segeval` container.

**Tech Stack:** Nextflow DSL2, Python 3.11 (numpy, scipy, pandas, scikit-image, scikit-learn, aicsimageio, tifffile, xmltodict), pytest, nf-test, Docker.

## Global Constraints

- Nextflow `>=25.04.0`; DSL2; uses `nf-boost` plugin.
- Every process: `tag`, a `label` resource class, an immutable-tag `container` (never `:latest`), `versions.yml` emit, a `*.size.csv` trace, and a `stub:` block.
- Tool arguments live in `conf/modules.config` via `ext.args` — never hardcoded in process scripts.
- `bin/*.py` invoked **by bare name** from a process MUST be git mode `100755` (`git update-index --chmod=+x`, verify `git ls-files -s` shows `100755`); import-only `bin/utils/*` stay `100644`.
- Gitmoji prefix on every commit (`:sparkles:`, `:white_check_mark:`, `:memo:`, `:hammer:`, `:recycle:`, `:fire:` …); use `:shortcode:` form.
- CSE `fast` (optimized) path must reproduce the `exact` (upstream) path within **1e-6** on every metric and `QualityScore`. No subsampling, no approximation.
- 2D only (`single_method_eval`), never the 3D path.
- Purpose is informational QC: no gating, no branching, no method-selection.
- All work happens in worktree `/Users/valer/Desktop/Github/mirage-cellseg-wt` on branch `cellseg-evaluator-integration`.
- Upstream source to vendor from: `/Users/valer/Desktop/Github/CellSegmentationEvaluator-master/pip package/CellSegmentationEvaluator/` (v1.5.19).
- Commands below assume CWD is the worktree root unless stated otherwise.

---

## File Structure

**Create:**
- `bin/utils/cse/__init__.py` — package marker, exposes `single_method_eval`.
- `bin/utils/cse/functions.py` — vendored metric helpers (patched in Phase B).
- `bin/utils/cse/single_method_eval.py` — vendored 2D entry (with the `img_thresholded` fix).
- `bin/utils/cse/LICENSE`, `bin/utils/cse/NOTICE` — upstream license + citation.
- `bin/seg_quality_eval.py` — Nextflow entry script (exec `100755`).
- `bin/merge_seg_eval.py` — merges per-patient JSONs → CSV (exec `100755`).
- `modules/local/seg_quality_eval.nf` — the `SEG_QUALITY_EVAL` process.
- `modules/local/merge_seg_eval.nf` — the `MERGE_SEG_EVAL` process.
- `containers/segeval/Dockerfile`, `containers/segeval/requirements.txt`.
- `tests/test_cse_equivalence.py` — golden-snapshot equivalence pytest.
- `tests/data/cse/` — golden JSON snapshot + a pointer/copy of the example fixture.
- `tests/modules/seg_quality_eval/` and `tests/modules/merge_seg_eval/` — nf-test stubs.

**Modify:**
- `subworkflows/local/postprocess.nf` — call the two processes, join channels, add `emit`, mix versions/size logs.
- `workflows/mirage.nf:158-195` — QC accumulator + `GENERATE_QC_REPORT` call.
- `modules/local/generate_qc_report.nf:7-36` — new input + CLI flag.
- `bin/generate_qc_report.py` — accept `--seg-eval` dir and render a section.
- `conf/modules.config` — `withName` blocks for both new processes.
- `nextflow.config` — param defaults.
- `nextflow_schema.json` — param docs.
- `containers/README.md` — new image row.
- `CLAUDE.md` / `docs/` — brief usage note.
- `.github/workflows/ci.yml` — run the equivalence pytest + new nf-tests.

---

## Phase A — Vendor CSE + equivalence oracle

### Task 1: Vendor upstream CSE 2D code verbatim

**Files:**
- Create: `bin/utils/cse/__init__.py`, `bin/utils/cse/functions.py`, `bin/utils/cse/single_method_eval.py`, `bin/utils/cse/LICENSE`, `bin/utils/cse/NOTICE`
- Test: `tests/test_cse_equivalence.py` (import smoke only in this task)

**Interfaces:**
- Produces: `from bin.utils.cse import single_method_eval` with signature
  `single_method_eval(img: dict, mask: dict, PCA_model, output_dir: Path, pixelsizex: float, pixelsizey: float) -> dict`,
  where `img`/`mask` are dicts `{"name": str, "img": AICSImage, "data": np.ndarray (TCZYX)}`.
  Returns a nested metrics dict including key `"QualityScore"`.

- [ ] **Step 1: Copy upstream files verbatim**

```bash
SRC="/Users/valer/Desktop/Github/CellSegmentationEvaluator-master/pip package/CellSegmentationEvaluator"
mkdir -p bin/utils/cse
cp "$SRC/functions.py"           bin/utils/cse/functions.py
cp "$SRC/single_method_eval.py"  bin/utils/cse/single_method_eval.py
cp "/Users/valer/Desktop/Github/CellSegmentationEvaluator-master/LICENSE" bin/utils/cse/LICENSE
```

- [ ] **Step 2: Write the package `__init__.py`** (upstream's does not re-export the functions)

```python
# bin/utils/cse/__init__.py
"""Vendored subset of CellSegmentationEvaluator v1.5.19 (2D path only).

Upstream: Chen & Murphy, "Evaluation of cell segmentation methods without
reference segmentations", Mol. Biol. Cell 34.6 (2023) ar50.
See LICENSE and NOTICE in this directory. Patched for bit-exact vectorized
performance; see docs/superpowers/plans for the equivalence contract.
"""
from .single_method_eval import single_method_eval

__all__ = ["single_method_eval"]
__cse_upstream_version__ = "1.5.19"
```

- [ ] **Step 3: Write `NOTICE`** with the required citation

```text
This directory vendors code from CellSegmentationEvaluator (CSE) v1.5.19
by Haoran Chen, Ted Zhang, and Robert F. Murphy, Carnegie Mellon University.

Original source: https://github.com/murphygroup/CellSegmentationEvaluator
License: see ./LICENSE

Citation:
Chen, Haoran, and Robert F. Murphy. "Evaluation of cell segmentation methods
without reference segmentations." Molecular Biology of the Cell 34.6 (2023):
ar50. https://doi.org/10.1091/mbc.E22-08-0364

Modifications by the Mirage project: removed interactive input()/sys.exit
control flow, fixed an unset `img_thresholded` reference, and replaced per-cell
Python loops with scipy.ndimage labeled reductions (bit-exact within 1e-6).
```

- [ ] **Step 4: Fix the `img_thresholded` unset-variable bug in `single_method_eval.py`**

The upstream `try/except` only assigns `img_thresholded` in the `except` branch; when OME metadata *does* provide seg-channel names it stays unset and line 96 raises `NameError`. Replace the block (upstream lines 70-89) so `img_thresholded` is always computed:

```python
	# separate image foreground background
	try:
		img_xmldict = xmltodict.parse(img["img"].metadata.to_xml())
		seg_channel_names = img_xmldict["OME"]["StructuredAnnotations"]["XMLAnnotation"]["Value"][
			"OriginalMetadata"
		]["Value"]
		all_channel_names = img["img"].get_channel_names()
		nuclear_channel_index = all_channel_names.index(seg_channel_names["Nucleus"])
		cell_channel_index = all_channel_names.index(seg_channel_names["Cell"])
		thresholding_channels = [nuclear_channel_index, cell_channel_index]
		seg_channel_provided = True
	except Exception:
		thresholding_channels = range(img["data"].shape[1])
		seg_channel_provided = False
	img_thresholded = sum(
		thresholding(np.squeeze(img["data"][0, c, 0, :, :]))
		for c in thresholding_channels
	)
	if not seg_channel_provided:
		img_thresholded[img_thresholded <= round(img["data"].shape[1] * 0.1)] = 0
```

- [ ] **Step 5: Remove the interactive/`sys.exit` pixel-size path**

Upstream lines 126-134 call `sys.exit()` / `get_pixel_area` when pixel sizes are missing. Since the entry script always passes explicit pixel sizes, replace with a hard requirement:

```python
			if not pixelsizex or not pixelsizey:
				raise ValueError(
					"single_method_eval requires explicit pixelsizex and pixelsizey (um/px)"
				)
			pixel_size = pixelsizex * pixelsizey
```

- [ ] **Step 5b: Make the `aicsimageio` import lazy in `functions.py`**

`functions.py` imports `from aicsimageio import AICSImage` at module top (upstream line 7), but `AICSImage` is used **only** inside `get_pixel_area` and `get_voxel_volume` — helpers the 2D path never calls when explicit pixel sizes are supplied. Requiring aicsimageio at import time forces a heavy dependency (unavailable in the base test env) on every consumer. Delete the top-level import and move it into the two helpers:

Delete upstream line 7 (`from aicsimageio import AICSImage`). Then inside `get_pixel_area` (upstream line 517) and `get_voxel_volume` (upstream line 514), add as the first line of each function body:

```python
	from aicsimageio import AICSImage  # lazy: only needed when reading pixel size from an image
```

This keeps the metric path pure-numpy/scipy/skimage/sklearn and importable without aicsimageio. (Verified: the vendored code runs end-to-end on synthetic input with aicsimageio absent.)

- [ ] **Step 6: Confirm import mode stays 100644 and write the import smoke test**

```python
# tests/test_cse_equivalence.py  (import smoke portion)
import importlib

def test_cse_imports():
    mod = importlib.import_module("bin.utils.cse")
    assert hasattr(mod, "single_method_eval")
    assert mod.__cse_upstream_version__ == "1.5.19"
```

- [ ] **Step 7: Run the import smoke test**

Run: `pytest tests/test_cse_equivalence.py::test_cse_imports -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add bin/utils/cse tests/test_cse_equivalence.py
git commit -m ":sparkles: Vendor CSE v1.5.19 2D metric code (headless-safe)"
```

### Task 2: Golden-snapshot equivalence harness

**Files:**
- Create: `tests/cse_fixture.py`, `tests/data/cse/golden_metrics.json`
- Modify: `tests/test_cse_equivalence.py`

**Interfaces:**
- Consumes: `bin.utils.cse.single_method_eval` (Task 1).
- Produces: `tests/cse_fixture.py::make_fixture() -> (img_dict, mask_dict, pixel_um)` (numpy-only, deterministic); pytest `assert_metrics_close(result, golden, tol=1e-6)` and `run_eval_on_fixture()` helpers reused by all Phase B tasks.

> **Why synthetic, not CSE's `example_data`:** those OME-TIFFs are Git-LFS pointer stubs (~130 bytes; the folder was unzipped without LFS and git-lfs is not installed), so no real image is available. A deterministic synthetic fixture is self-contained, commits no binaries, runs in the base env without `aicsimageio`, and exercises every code path (matching, KMeans, silhouette). This approach was validated end-to-end against the verbatim vendored code (`QualityScore` computed over 121 synthetic cells).

- [ ] **Step 1: Write the deterministic synthetic fixture generator**

```python
# tests/cse_fixture.py
"""Deterministic synthetic multichannel image + cell/nucleus masks for CSE tests.

CSE's own example_data are Git-LFS stubs, so we synthesize a small labeled scene:
a grid of square cells (each with a centered nucleus of the same label) whose
3 channels separate the cells into 3 intensity types — enough to exercise
matching, KMeans k=2..10, and silhouette.
"""
import numpy as np

PIXEL_UM = 0.5

def make_arrays():
    rng = np.random.default_rng(0)
    Y = X = 160
    C = 3
    cell = np.zeros((Y, X), np.int32)
    nuc = np.zeros((Y, X), np.int32)
    img = np.zeros((C, Y, X), np.float32)
    cid = 0
    for gy in range(6, Y - 12, 14):
        for gx in range(6, X - 12, 14):
            cid += 1
            cell[gy:gy + 10, gx:gx + 10] = cid
            nuc[gy + 3:gy + 7, gx + 3:gx + 7] = cid
            t = cid % C
            for c in range(C):
                img[c, gy:gy + 10, gx:gx + 10] = (
                    50 + (80 if c == t else 5) + rng.normal(0, 3, (10, 10))
                )
    return img, cell, nuc

def make_fixture():
    img, cell, nuc = make_arrays()
    img5 = img[np.newaxis, :, np.newaxis, :, :]                    # (1,C,1,Y,X)
    mask5 = np.stack([cell, nuc], 0)[np.newaxis, :, np.newaxis, :, :]  # (1,2,1,Y,X)
    img_d = {"name": "synth", "img": None, "data": img5}
    mask_d = {"name": "synth", "img": None, "data": mask5}
    return img_d, mask_d, PIXEL_UM
```

- [ ] **Step 2: Write the fixture-runner + comparison helpers** (numpy only — no aicsimageio)

```python
# tests/test_cse_equivalence.py  (append)
import json
from pathlib import Path
import numpy as np
from tests.cse_fixture import make_fixture
from bin.utils.cse import single_method_eval

DATA = Path(__file__).parent / "data" / "cse"

def run_eval_on_fixture():
    img, mask, px = make_fixture()
    return single_method_eval(img, mask, PCA_model=False, output_dir=".",
                              pixelsizex=px, pixelsizey=px)

def flatten(metrics):
    flat = {}
    for k, v in metrics.items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                flat[f"{k}::{kk}"] = float(vv)
        else:
            flat[k] = float(v)
    return flat

def assert_metrics_close(result, golden, tol=1e-6):
    r, g = flatten(result), dict(golden)
    assert set(r) == set(g), f"metric keys differ: {set(r) ^ set(g)}"
    for key in g:
        if np.isnan(g[key]):
            assert np.isnan(r[key]), f"{key}: expected NaN"
        else:
            assert abs(r[key] - g[key]) <= tol, f"{key}: {r[key]} vs {g[key]}"
```

- [ ] **Step 3: Generate the golden snapshot from the verbatim vendored code**

Because Task 1 vendored upstream *verbatim* (only removing dead control flow + lazy import), its output IS the upstream reference. Generate and commit it now, before any Phase B optimization:

```bash
mkdir -p tests/data/cse
python - <<'PY'
import json
from tests.test_cse_equivalence import run_eval_on_fixture, flatten
from pathlib import Path
m = flatten(run_eval_on_fixture())
Path("tests/data/cse/golden_metrics.json").write_text(json.dumps(m, indent=2, sort_keys=True))
print("wrote", len(m), "metrics")
PY
```

- [ ] **Step 4: Write the equivalence test that loads the golden file**

```python
# tests/test_cse_equivalence.py  (append)
def test_fast_matches_golden():
    golden = json.loads((DATA / "golden_metrics.json").read_text())
    result = run_eval_on_fixture()
    assert_metrics_close(result, golden, tol=1e-6)
```

- [ ] **Step 5: Run it — verbatim code trivially matches its own snapshot**

Run: `pytest tests/test_cse_equivalence.py::test_fast_matches_golden -v`
Expected: PASS. (This is now the guard for every Phase B change.)

- [ ] **Step 6: Commit**

```bash
git add tests/cse_fixture.py tests/data/cse tests/test_cse_equivalence.py
git commit -m ":white_check_mark: Add CSE golden-snapshot equivalence test"
```

---

## Phase B — Bit-exact optimizations (each guarded by `test_fast_matches_golden`)

### Task 3: Vectorize `cell_type` per-cell intensity loop

**Files:**
- Modify: `bin/utils/cse/functions.py` (`cell_type`, upstream lines 349-363, 2D branch)

**Interfaces:**
- Consumes: golden test from Task 2.
- Produces: unchanged `cell_type(mask, channels) -> list[np.ndarray]` signature and return order (row order = ascending cell label).

- [ ] **Step 1: Confirm the loop is a labeled mean.** For 2D, `single_cell_intensity_z = np.sum(channel_z[tuple(cell_coord[j])]) / cell_size_current` is exactly the mean of `channel_z` over the pixels of cell `j`. `scipy.ndimage.mean(channel_z, labels=mask, index=ids)` computes the same value; ordering matches because `get_indices_pandas(mask)[1:]` and `np.unique(mask)[1:]` are both ascending label order.

- [ ] **Step 2: Rewrite the 2D branch** (replace upstream lines 349-361)

```python
	else:
		ids = np.unique(mask)
		ids = ids[ids != 0]
		for i in range(n):
			channel = channels[i]
			channel_z = ss.fit_transform(channel)
			# labeled mean == np.sum(channel_z[cell_pixels]) / cell_size, per cell,
			# in ascending-label order (matches get_indices_pandas[1:]).
			cell_intensity_z = ndimage.mean(channel_z, labels=mask, index=ids)
			feature_matrix_z_pieces.append(list(cell_intensity_z))
```

- [ ] **Step 3: Add the import** at the top of `functions.py`

```python
from scipy import ndimage
```

- [ ] **Step 4: Run the equivalence test**

Run: `pytest tests/test_cse_equivalence.py::test_fast_matches_golden -v`
Expected: PASS within 1e-6.

- [ ] **Step 5: Commit**

```bash
git add bin/utils/cse/functions.py
git commit -m ":zap: Vectorize CSE cell_type per-cell loop (ndimage, bit-exact)"
```

### Task 4: Vectorize `cell_uniformity` per-cell loops

**Files:**
- Modify: `bin/utils/cse/functions.py` (`cell_uniformity`, upstream lines 402-418, 2D branch)

**Interfaces:**
- Produces: unchanged `cell_uniformity(mask, channels, label_list) -> (CV, fraction, silhouette)`.

- [ ] **Step 1: Rewrite the 2D branch** (replace upstream lines 402-418). Both `feature_matrix` (raw channel) and `feature_matrix_z` (standardized) are per-cell means → two labeled reductions:

```python
	else:
		ids = np.unique(mask)
		ids = ids[ids != 0]
		for i in range(n):
			channel = channels[i]
			channel_z = ss.fit_transform(channel)
			cell_intensity   = ndimage.mean(channel,   labels=mask, index=ids)
			cell_intensity_z = ndimage.mean(channel_z, labels=mask, index=ids)
			feature_matrix_pieces.append(list(cell_intensity))
			feature_matrix_z_pieces.append(list(cell_intensity_z))
```

- [ ] **Step 2: Run the equivalence test**

Run: `pytest tests/test_cse_equivalence.py::test_fast_matches_golden -v`
Expected: PASS within 1e-6.

- [ ] **Step 3: Commit**

```bash
git add bin/utils/cse/functions.py
git commit -m ":zap: Vectorize CSE cell_uniformity per-cell loops (ndimage, bit-exact)"
```

### Task 5: Vectorize the per-pixel nucleus lookup in `get_matched_masks`

**Files:**
- Modify: `bin/utils/cse/functions.py` (`get_matched_masks`, upstream line 588)

**Interfaces:**
- Produces: unchanged `get_matched_masks(cell_mask, nuclear_mask) -> (cell_matched, nuclear_matched, cell_outside_nucleus)`.

- [ ] **Step 1: Replace the per-pixel Python `map`** (upstream line 588). `current_cell_coords` is an `(npix, 2)` int array; the candidate nucleus IDs under the cell are a fancy-index + `np.unique`:

```python
			nuclear_search_num = np.unique(
				nuclear_mask[current_cell_coords[:, 0], current_cell_coords[:, 1]]
			)
```

This yields the identical sorted unique set; the greedy assignment loop over `nuclear_search_num` is unchanged, so ordering and results are preserved.

- [ ] **Step 2: Run the equivalence test**

Run: `pytest tests/test_cse_equivalence.py::test_fast_matches_golden -v`
Expected: PASS within 1e-6.

- [ ] **Step 3: Commit**

```bash
git add bin/utils/cse/functions.py
git commit -m ":zap: Vectorize CSE nucleus lookup in get_matched_masks (bit-exact)"
```

### Task 6: Run the independent k=2..10 clustering passes concurrently

**Files:**
- Modify: `bin/utils/cse/functions.py` (`cell_type` KMeans loop, upstream lines 365-371)

**Interfaces:**
- Produces: unchanged `cell_type` return (`label_list[0]` = zeros, `label_list[c-1]` = KMeans(c).labels_ for c=2..10).

- [ ] **Step 1: Parallelize the KMeans fits across `c`** — each `c` is independent; results slot back by index so order is preserved. Replace upstream lines 365-370:

```python
	from concurrent.futures import ThreadPoolExecutor
	label_list = [np.zeros(cell_coord_num, dtype=int)]

	def _fit(c):
		return KMeans(n_clusters=c, random_state=777).fit(feature_matrix_z).labels_.astype(int)

	with ThreadPoolExecutor(max_workers=min(9, (os.cpu_count() or 1))) as ex:
		for labels in ex.map(_fit, range(2, 11)):
			label_list.append(labels)
```

`random_state=777` keeps each fit deterministic regardless of scheduling, so results are bit-identical.

- [ ] **Step 2: Add `import os`** at the top of `functions.py` if not already present.

- [ ] **Step 3: Run the equivalence test**

Run: `pytest tests/test_cse_equivalence.py::test_fast_matches_golden -v`
Expected: PASS within 1e-6.

- [ ] **Step 4: Commit**

```bash
git add bin/utils/cse/functions.py
git commit -m ":zap: Run CSE k=2..10 KMeans fits concurrently (deterministic)"
```

---

## Phase C — Nextflow entry + merge scripts

### Task 7: `bin/seg_quality_eval.py` — mask stacking + eval

**Files:**
- Create: `bin/seg_quality_eval.py` (exec `100755`)
- Test: `tests/test_seg_quality_eval_cli.py`

**Interfaces:**
- Consumes: `bin.utils.cse.single_method_eval`.
- Produces: CLI `seg_quality_eval.py --cell-mask C --nuclei-mask N --image IMG --id ID --out OUT.json [--pixel-size-um F]`; writes a JSON `{"id": ID, "metrics": {...}, "QualityScore": float}`.

- [ ] **Step 1: Write the failing CLI test**

```python
# tests/test_seg_quality_eval_cli.py
import json, subprocess, sys
from pathlib import Path
import numpy as np
import tifffile
from tests.cse_fixture import make_arrays

def test_cli_writes_quality_score(tmp_path):
    # Synthetic fixture: img (C,Y,X), cell/nuc label masks (Y,X). Write as the
    # separate label TIFFs + multichannel image that Mirage's SEGMENT emits.
    img, cell, nuc = make_arrays()
    cp = tmp_path / "p_cell_mask.tif"
    npth = tmp_path / "p_nuclei_mask.tif"
    imgp = tmp_path / "p_image.tif"
    tifffile.imwrite(cp, cell.astype(np.int32))
    tifffile.imwrite(npth, nuc.astype(np.int32))
    tifffile.imwrite(imgp, img.astype(np.float32))   # (C,Y,X)
    out = tmp_path / "p_seg_eval.json"
    subprocess.run([sys.executable, "bin/seg_quality_eval.py",
                    "--cell-mask", str(cp), "--nuclei-mask", str(npth),
                    "--image", str(imgp),
                    "--id", "p", "--out", str(out),
                    "--pixel-size-um", "0.5"], check=True)
    doc = json.loads(out.read_text())
    assert doc["id"] == "p"
    assert isinstance(doc["QualityScore"], float)
```

- [ ] **Step 2: Run it to confirm failure**

Run: `pytest tests/test_seg_quality_eval_cli.py -v`
Expected: FAIL (`bin/seg_quality_eval.py` does not exist).

- [ ] **Step 3: Write the script**

```python
#!/usr/bin/env python3
"""Evaluate one patient's cell segmentation with vendored CSE (2D)."""
import argparse, json, os, sys
import numpy as np
import tifffile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "utils"))
from cse import single_method_eval  # noqa: E402


def _read_image_cyx(path):
    """Return (channels (C,Y,X), pixel_um_or_None). Prefer aicsimageio for
    robust OME axis handling (present in the container); fall back to tifffile."""
    try:
        from aicsimageio import AICSImage
        a = AICSImage(path)
        data = np.asarray(a.get_image_data("CYX"))   # T,Z are 1 for 2D WSI
        ps = a.physical_pixel_sizes
        px = float(ps.X) if (ps.X and ps.Y) else None
        return data, px
    except Exception:
        arr = np.asarray(tifffile.imread(path))
        if arr.ndim == 2:
            arr = arr[np.newaxis, :, :]
        elif arr.ndim == 3 and arr.shape[-1] <= 5 and arr.shape[0] > 5:
            arr = np.moveaxis(arr, -1, 0)            # YXC -> CYX heuristic
        return arr, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell-mask", required=True)
    ap.add_argument("--nuclei-mask", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pixel-size-um", default=None)
    a = ap.parse_args()

    channels, px_meta = _read_image_cyx(a.image)             # (C,Y,X)
    img_data = channels[np.newaxis, :, np.newaxis, :, :]     # (1,C,1,Y,X)
    # img["img"]=None forces CSE's metadata-free thresholding path, matching the
    # golden equivalence fixture exactly.
    img = {"name": a.id, "img": None, "data": img_data}

    cell = tifffile.imread(a.cell_mask).astype(np.int32)
    nuc = tifffile.imread(a.nuclei_mask).astype(np.int32)
    # CSE mask["data"] is (T,C,Z,Y,X) with C-axis: ch0=cell, ch1=nucleus.
    mask_data = np.stack([cell, nuc], 0)[np.newaxis, :, np.newaxis, :, :]  # (1,2,1,Y,X)
    mask = {"name": a.id, "img": None, "data": mask_data}

    if a.pixel_size_um:
        px = py = float(a.pixel_size_um)
    elif px_meta:
        px = py = px_meta
    else:
        raise SystemExit("Pixel size missing from image metadata; pass --pixel-size-um")

    metrics = single_method_eval(img, mask, PCA_model=False, output_dir=".",
                                 pixelsizex=px, pixelsizey=py)
    qs = metrics.get("QualityScore")
    doc = {"id": a.id, "metrics": metrics,
           "QualityScore": float(qs) if qs is not None else float("nan")}
    with open(a.out, "w") as fh:
        json.dump(doc, fh, indent=2, default=lambda o: float(o))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Make it executable in git and run the test**

```bash
chmod +x bin/seg_quality_eval.py
git update-index --add --chmod=+x bin/seg_quality_eval.py 2>/dev/null || true
pytest tests/test_seg_quality_eval_cli.py -v
```

Expected: PASS.

- [ ] **Step 5: Verify the git exec bit**

Run: `git ls-files -s bin/seg_quality_eval.py`
Expected: mode `100755`.

- [ ] **Step 6: Commit**

```bash
git add bin/seg_quality_eval.py tests/test_seg_quality_eval_cli.py
git commit -m ":sparkles: Add seg_quality_eval.py CSE entry script"
```

### Task 8: `bin/merge_seg_eval.py` — JSONs → CSV

**Files:**
- Create: `bin/merge_seg_eval.py` (exec `100755`)
- Test: `tests/test_merge_seg_eval.py`

**Interfaces:**
- Consumes: the per-patient JSON shape from Task 7.
- Produces: CLI `merge_seg_eval.py --inputs a.json b.json --out segmentation_metrics.csv`; one row per input, columns = `id`, `QualityScore`, and every flattened metric.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_merge_seg_eval.py
import csv, json, subprocess, sys
from pathlib import Path

def test_merge_two(tmp_path):
    for pid, qs in [("p1", 0.5), ("p2", 0.7)]:
        (tmp_path / f"{pid}.json").write_text(json.dumps(
            {"id": pid, "QualityScore": qs,
             "metrics": {"Matched Cell": {"NumberOfCellsPer100SquareMicrons": 1.0}}}))
    out = tmp_path / "segmentation_metrics.csv"
    subprocess.run([sys.executable, "bin/merge_seg_eval.py",
                    "--inputs", str(tmp_path/"p1.json"), str(tmp_path/"p2.json"),
                    "--out", str(out)], check=True)
    rows = list(csv.DictReader(out.open()))
    assert {r["id"] for r in rows} == {"p1", "p2"}
    assert any(r["QualityScore"] == "0.7" for r in rows)
```

- [ ] **Step 2: Run it — confirm failure**

Run: `pytest tests/test_merge_seg_eval.py -v`
Expected: FAIL (script missing).

- [ ] **Step 3: Write the script**

```python
#!/usr/bin/env python3
"""Merge per-patient CSE JSONs into one CSV (one row per patient)."""
import argparse, csv, json


def flatten(doc):
    row = {"id": doc["id"], "QualityScore": doc.get("QualityScore")}
    for k, v in doc.get("metrics", {}).items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                row[f"{k}::{kk}"] = vv
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rows = [flatten(json.loads(open(p).read())) for p in a.inputs]
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Make executable + run**

```bash
chmod +x bin/merge_seg_eval.py
git update-index --add --chmod=+x bin/merge_seg_eval.py 2>/dev/null || true
pytest tests/test_merge_seg_eval.py -v
```

Expected: PASS.

- [ ] **Step 5: Verify exec bit + commit**

```bash
git ls-files -s bin/merge_seg_eval.py   # expect 100755
git add bin/merge_seg_eval.py tests/test_merge_seg_eval.py
git commit -m ":sparkles: Add merge_seg_eval.py (per-patient JSON -> CSV)"
```

---

## Phase D — Container

### Task 9: `segeval` container

**Files:**
- Create: `containers/segeval/Dockerfile`, `containers/segeval/requirements.txt`
- Modify: `containers/README.md`

**Interfaces:**
- Produces: image `ghcr.io/sceriff0/mirage/segeval:<tag>` carrying numpy/scipy/pandas/scikit-image/scikit-learn/aicsimageio/tifffile/xmltodict.

- [ ] **Step 1: Write pinned `requirements.txt`** (versions aligned with the segmentation/quantification containers)

```text
numpy==1.26.4
scipy==1.11.4
pandas==2.2.3
scikit-image==0.25.2
scikit-learn==1.5.2
aicsimageio==4.14.0
aicsimageio[all]==4.14.0
tifffile==2023.4.12
xmltodict==0.13.0
```

- [ ] **Step 2: Write `Dockerfile`**

```dockerfile
FROM python:3.11-slim

LABEL description="Mirage SEG_QUALITY_EVAL: vendored CellSegmentationEvaluator (2D) metrics"

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir "numpy<2.0"

CMD ["python3"]
```

- [ ] **Step 3: Build locally to verify it resolves**

Run: `docker build -t mirage/segeval:dev containers/segeval`
Expected: build succeeds; `docker run --rm mirage/segeval:dev python3 -c "import scipy.ndimage, sklearn, aicsimageio, xmltodict; print('ok')"` prints `ok`.

- [ ] **Step 4: Add the README row** to `containers/README.md` table (match existing column format)

```markdown
| `segeval` | `ghcr.io/sceriff0/mirage/segeval:<tag>` | `SEG_QUALITY_EVAL`, `MERGE_SEG_EVAL` | `python:3.11-slim` + numpy/scipy/pandas/scikit-image/scikit-learn/aicsimageio/tifffile/xmltodict (vendored CSE metrics) |
```

- [ ] **Step 5: Commit**

```bash
git add containers/segeval containers/README.md
git commit -m ":hammer: Add pinned segeval container for CSE metrics"
```

---

## Phase E — Nextflow process + wiring

### Task 10: `SEG_QUALITY_EVAL` process + config + params

**Files:**
- Create: `modules/local/seg_quality_eval.nf`
- Modify: `conf/modules.config`, `nextflow.config`, `nextflow_schema.json`
- Test: `tests/modules/seg_quality_eval/main.nf.test`

**Interfaces:**
- Consumes: `tuple val(meta), path(cell_mask), path(nuclei_mask), path(image)`.
- Produces: `emit: metrics = tuple val(meta), path("*_seg_eval.json")`; `emit: versions = path "versions.yml"`; `emit: size_log = path("*.size.csv")`.

- [ ] **Step 1: Write the process** (modeled on `modules/local/seg_qc_geojson.nf`)

```groovy
/*
 * SEG_QUALITY_EVAL - reference-free cell-segmentation quality scoring (CSE, 2D).
 * Scores each patient's cell+nucleus mask against its reference image and emits
 * a per-patient metrics JSON (informational QC; no gating).
 */
process SEG_QUALITY_EVAL {
    tag "${meta.patient_id}"
    label 'process_high'

    container "ghcr.io/sceriff0/mirage/segeval:${params.segeval_tag ?: 'dev'}"

    input:
    tuple val(meta), path(cell_mask), path(nuclei_mask), path(image)

    output:
    tuple val(meta), path("*_seg_eval.json"), emit: metrics
    path "versions.yml"                      , emit: versions
    path("*.size.csv")                       , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def prefix = "${meta.patient_id}"
    def px_arg = params.cse_pixel_size_um ? "--pixel-size-um ${params.cse_pixel_size_um}" : ''
    """
    bytes=\$(stat -L --printf="%s" ${image} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${image.name},\${bytes}" > ${prefix}.SEG_QUALITY_EVAL.size.csv

    seg_quality_eval.py \\
        --cell-mask ${cell_mask} \\
        --nuclei-mask ${nuclei_mask} \\
        --image ${image} \\
        --id ${prefix} \\
        --out ${prefix}_seg_eval.json \\
        ${px_arg} \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python3 --version 2>&1 | sed 's/Python //')
        scikit-learn: \$(python3 -c "import sklearn; print(sklearn.__version__)" 2>/dev/null || echo "unknown")
        scipy: \$(python3 -c "import scipy; print(scipy.__version__)" 2>/dev/null || echo "unknown")
        CellSegmentationEvaluator: \$(python3 -c "import sys,os; sys.path.insert(0, os.path.join('.','utils')); import cse; print(cse.__cse_upstream_version__)" 2>/dev/null || echo "1.5.19-vendored")
    END_VERSIONS
    """

    stub:
    def prefix = "${meta.patient_id}"
    """
    echo '{"id": "${prefix}", "QualityScore": 0.0, "metrics": {}}' > ${prefix}_seg_eval.json
    echo "STUB,${meta.patient_id},stub,0" > ${prefix}.SEG_QUALITY_EVAL.size.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        CellSegmentationEvaluator: 1.5.19-vendored
    END_VERSIONS
    """
}
```

- [ ] **Step 2: Add the `conf/modules.config` block** (after the `WARP_SEG_QC` block, ~line 300)

```groovy
    withName: 'SEG_QUALITY_EVAL' {
        cpus   = 8
        memory = {
            def file_gb = (image.size() >> 30) ?: 1
            (file_gb < 10 ? 32.GB : file_gb < 30 ? 64.GB : 128.GB) * task.attempt
        }
        time   = { 4.h * task.attempt }
        ext.when = { !params.skip_seg_quality_eval }
        ext.args = { params.cse_fast ? '' : '--exact' }
        publishDir = [
            path: { "${params.outdir}/${meta.patient_id}/qc/segmentation" },
            mode: 'copy',
            pattern: "*_seg_eval.json"
        ]
    }
```

- [ ] **Step 3: Add param defaults** to `nextflow.config` `params { ... }`

```groovy
    skip_seg_quality_eval = false
    cse_fast              = true
    cse_pixel_size_um     = null
    segeval_tag           = 'dev'
```

- [ ] **Step 4: Document the params** in `nextflow_schema.json` (add to the postprocessing group: `skip_seg_quality_eval` boolean default false; `cse_fast` boolean default true; `cse_pixel_size_um` number; `segeval_tag` string). Follow the format of an existing `skip_*` entry.

- [ ] **Step 5: Write the nf-test stub**

```groovy
// tests/modules/seg_quality_eval/main.nf.test
nextflow_process {
    name "Test SEG_QUALITY_EVAL"
    script "modules/local/seg_quality_eval.nf"
    process "SEG_QUALITY_EVAL"
    options "-stub"

    test("stub emits metrics json + versions") {
        when {
            process {
                """
                input[0] = [ [patient_id: 'p1', is_reference: true],
                             file('cell.tif'), file('nuc.tif'), file('img.ome.tiff') ]
                """
            }
        }
        then {
            assert process.success
            assert process.out.metrics
            assert process.out.versions
        }
    }
}
```

- [ ] **Step 6: Run the stub test**

Run: `nf-test test tests/modules/seg_quality_eval/main.nf.test --profile test,docker`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add modules/local/seg_quality_eval.nf conf/modules.config nextflow.config nextflow_schema.json tests/modules/seg_quality_eval
git commit -m ":sparkles: Add SEG_QUALITY_EVAL process, config, and params"
```

### Task 11: `MERGE_SEG_EVAL` process

**Files:**
- Create: `modules/local/merge_seg_eval.nf`
- Modify: `conf/modules.config`
- Test: `tests/modules/merge_seg_eval/main.nf.test`

**Interfaces:**
- Consumes: `path(seg_eval_jsons)` (collected list).
- Produces: `emit: csv = path("segmentation_metrics.csv")`; `emit: versions = path "versions.yml"`.

- [ ] **Step 1: Write the process**

```groovy
/*
 * MERGE_SEG_EVAL - concatenate per-patient CSE metric JSONs into one CSV.
 */
process MERGE_SEG_EVAL {
    tag "seg_eval_merge"
    label 'process_low'

    container "ghcr.io/sceriff0/mirage/segeval:${params.segeval_tag ?: 'dev'}"

    input:
    path(seg_eval_jsons)

    output:
    path "segmentation_metrics.csv", emit: csv
    path "versions.yml"            , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    """
    merge_seg_eval.py --inputs ${seg_eval_jsons} --out segmentation_metrics.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python3 --version 2>&1 | sed 's/Python //')
    END_VERSIONS
    """

    stub:
    """
    printf 'id,QualityScore\\np1,0.0\\n' > segmentation_metrics.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
    END_VERSIONS
    """
}
```

- [ ] **Step 2: Add the `conf/modules.config` block**

```groovy
    withName: 'MERGE_SEG_EVAL' {
        cpus   = 1
        memory = { 4.GB * task.attempt }
        time   = { 1.h * task.attempt }
        ext.when = { !params.skip_seg_quality_eval }
        publishDir = [
            path: { "${params.outdir}/qc/segmentation" },
            mode: 'copy',
            pattern: "*.csv"
        ]
    }
```

- [ ] **Step 3: Write + run the nf-test stub** (same shape as Task 10 Step 5, input `[ file('a.json'), file('b.json') ]`, assert `process.out.csv`).

Run: `nf-test test tests/modules/merge_seg_eval/main.nf.test --profile test,docker`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add modules/local/merge_seg_eval.nf conf/modules.config tests/modules/merge_seg_eval
git commit -m ":sparkles: Add MERGE_SEG_EVAL process"
```

### Task 12: Wire both processes into `POSTPROCESSING`

**Files:**
- Modify: `subworkflows/local/postprocess.nf`

**Interfaces:**
- Consumes: `SEGMENT.out.cell_mask`, `SEGMENT.out.nuclei_mask`, `ch_references` (all keyed by `meta.patient_id`).
- Produces: `POSTPROCESSING.out.seg_eval_metrics` (the merged CSV channel).

- [ ] **Step 1: Add the includes** at the top of `postprocess.nf` (with the other `include` lines)

```groovy
include { SEG_QUALITY_EVAL } from '../../modules/local/seg_quality_eval.nf'
include { MERGE_SEG_EVAL   } from '../../modules/local/merge_seg_eval.nf'
```

- [ ] **Step 2: Build the joined input and call the process** (insert after `EXTRACT_CELL_PROPERTIES(ch_cell_mask)` at line 67). Join the two masks + the reference image on `patient_id`:

```groovy
    // ========================================================================
    // SEGMENTATION QUALITY EVAL (CSE) - informational per-patient QC
    // ========================================================================
    ch_seg_eval_in = ch_cell_mask
        .map { meta, cmask -> [meta.patient_id, meta, cmask] }
        .join(ch_nuclei_mask.map { meta, nmask -> [meta.patient_id, nmask] }, by: 0)
        .join(ch_references.map { meta, img -> [meta.patient_id, img] }, by: 0)
        .map { _pid, meta, cmask, nmask, img -> [meta, cmask, nmask, img] }

    SEG_QUALITY_EVAL(ch_seg_eval_in)

    MERGE_SEG_EVAL(
        SEG_QUALITY_EVAL.out.metrics.map { _meta, json -> json }.collect().ifEmpty([])
    )
    def ch_seg_eval_metrics = MERGE_SEG_EVAL.out.csv
```

- [ ] **Step 3: Mix versions + size logs** — add to the existing `ch_size_logs` (line 319) and `ch_versions` (line 340) chains:

```groovy
        .mix(SEG_QUALITY_EVAL.out.size_log)
```
```groovy
        .mix(SEG_QUALITY_EVAL.out.versions.first())
        .mix(MERGE_SEG_EVAL.out.versions.first())
```

- [ ] **Step 4: Add the emit** (in the `emit:` block, after line 355)

```groovy
    seg_eval_metrics  = ch_seg_eval_metrics
```

- [ ] **Step 5: Run the stub pipeline to confirm the subworkflow wires up**

Run: `nextflow run . -profile test,docker -stub --outdir results`
Expected: completes; `SEG_QUALITY_EVAL` and `MERGE_SEG_EVAL` appear in the process list.

- [ ] **Step 6: Commit**

```bash
git add subworkflows/local/postprocess.nf
git commit -m ":sparkles: Wire SEG_QUALITY_EVAL + MERGE_SEG_EVAL into POSTPROCESSING"
```

### Task 13: Surface the metrics in the final QC report

**Files:**
- Modify: `workflows/mirage.nf:158-195`, `modules/local/generate_qc_report.nf:7-36`, `bin/generate_qc_report.py`

**Interfaces:**
- Consumes: `POSTPROCESSING.out.seg_eval_metrics`.
- Produces: an extra positional input to `GENERATE_QC_REPORT` and a `--seg-eval` CLI flag.

- [ ] **Step 1: Add the accumulator + mix** in `mirage.nf` (near line 162 and inside the postprocessing `if` at 178-181)

```groovy
        def ch_seg_eval_csv         = Channel.empty()
```
```groovy
            ch_seg_eval_csv = ch_seg_eval_csv.mix(POSTPROCESSING.out.seg_eval_metrics)
```

- [ ] **Step 2: Pass it to `GENERATE_QC_REPORT`** (extend the call at 188-195 with a new final positional arg before `ch_collated_versions`)

```groovy
        GENERATE_QC_REPORT(
            ch_preprocess_qc_pngs.collect().ifEmpty([]),
            ch_registration_qc_pngs.collect().ifEmpty([]),
            ch_feature_dist_jsons.collect().ifEmpty([]),
            ch_valis_summary_csvs.collect().ifEmpty([]),
            ch_postprocess_qc_pngs.collect().ifEmpty([]),
            ch_seg_eval_csv.collect().ifEmpty([]),
            ch_collated_versions
        )
```

- [ ] **Step 3: Add the input + flag** to `generate_qc_report.nf` (new `path` input line after line 12, new CLI flag in the script after line 32)

```groovy
    path(seg_eval_csvs, stageAs: 'seg_eval/*')
```
```groovy
        --seg-eval seg_eval/ \\
```

- [ ] **Step 4: Handle `--seg-eval` in `bin/generate_qc_report.py`** — add the argparse option and a section that reads any `seg_eval/*.csv` and renders a table (id, QualityScore, metrics). Follow the existing `--feature-distances` handler's structure. Add the argument:

```python
    parser.add_argument("--seg-eval", default=None,
                        help="Directory of segmentation_metrics.csv from CSE")
```

and, where sections are assembled, append a "Segmentation Quality (CSE)" table built from the CSV rows (reuse the module's existing HTML-table helper).

- [ ] **Step 5: Run the stub pipeline end-to-end**

Run: `nextflow run . -profile test,docker -stub --outdir results`
Expected: completes; `mirage_qc_report_*.html` is produced without error.

- [ ] **Step 6: Commit**

```bash
git add workflows/mirage.nf modules/local/generate_qc_report.nf bin/generate_qc_report.py
git commit -m ":sparkles: Surface CSE segmentation-quality metrics in QC report"
```

---

## Phase F — Docs + CI

### Task 14: Docs + CI wiring

**Files:**
- Modify: `.github/workflows/ci.yml`, `CLAUDE.md` (or `docs/`)

- [ ] **Step 1: Add the equivalence + CLI pytests to CI** — ensure the python-tests job runs `tests/test_cse_equivalence.py`, `tests/test_seg_quality_eval_cli.py`, `tests/test_merge_seg_eval.py`. These need `numpy`, `scipy`, `scikit-image`, `scikit-learn`, `pandas`, `tifffile`, `xmltodict` in the CI python env (NOT `aicsimageio` — the tests use the synthetic numpy fixture and tifffile only; aicsimageio is a lazy, container-only dependency); add them to the CI pip install / test requirements file.

- [ ] **Step 2: Add the two nf-tests to the stub suite** — confirm `tests/modules/seg_quality_eval` and `tests/modules/merge_seg_eval` are picked up by the existing `nf-test test` invocation (they are, by directory convention; run once to confirm).

Run: `nf-test test tests/modules/seg_quality_eval tests/modules/merge_seg_eval --profile test,docker`
Expected: PASS.

- [ ] **Step 3: Document the feature** — add a short "Segmentation quality evaluation (CSE)" note to `CLAUDE.md`'s postprocessing description and/or `docs/`: what it does, the `skip_seg_quality_eval` / `cse_fast` / `cse_pixel_size_um` params, and where the CSV lands (`${outdir}/qc/segmentation/segmentation_metrics.csv`).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml CLAUDE.md docs 2>/dev/null
git commit -m ":memo: Document CSE seg-quality step and wire tests into CI"
```

---

## Self-Review

**Spec coverage:**
- §1 goal (informational per-patient QC) → Tasks 10, 12, 13.
- §3 integration point after SEGMENT → Task 12.
- §4.1 vendored patched CSE → Tasks 1, 3-6.
- §4.2 entry script + mask stacking + pixel-size → Task 7.
- §4.3 process → Task 10. §4.4 merge → Tasks 8, 11.
- §4.5 dedicated pinned container → Task 9.
- §4.6 QC aggregation wiring → Tasks 12, 13.
- §4.7 config/params/schema → Task 10.
- §5 bit-exact speedups (labeled reductions, vectorized lookup, concurrent clustering) → Tasks 3, 4, 5, 6.
- §6 equivalence pytest + nf-test stubs + smoke → Tasks 2, 10, 11, 12, 13, 14.
- §7 risks: pixel-size provenance → Task 7 Step 3 (`--pixel-size-um` fallback) + `cse_pixel_size_um` param; `img_thresholded` bug → Task 1 Step 4; mask index space → Task 7 (`_to_tczyx_labels` builds ch0=cell/ch1=nucleus as CSE expects).

**Placeholder scan:** the only intentionally-descriptive steps are Task 13 Step 4 (QC-report HTML section) and Task 14 Step 3 (docs), which depend on `bin/generate_qc_report.py`'s existing table helpers — the implementer must read that file; every code-bearing step ships real code.

**Type consistency:** `single_method_eval(img, mask, PCA_model, output_dir, pixelsizex, pixelsizey)` used identically in Tasks 1, 2, 7. JSON shape `{"id", "QualityScore", "metrics"}` produced in Task 7, consumed in Task 8. Emit names `metrics`/`versions`/`size_log` (Task 10) and `csv`/`versions` (Task 11) match their consumers in Task 12. `POSTPROCESSING.out.seg_eval_metrics` produced in Task 12, consumed in Task 13.

**Known-risk tasks (verify equivalence carefully):** Task 5 (nucleus lookup) and Task 6 (concurrent KMeans) touch matching/clustering; the golden test at 1e-6 is the gate — if either fails, revert that single commit and keep the exact path (correctness over speed).
