# Registration-Accuracy Evaluator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evaluate Mirage's VALIS registration accuracy against ANHIR/ACROBAT landmark ground truth — true landmark TRE for in-process tiling ON vs OFF, alongside VALIS's self-reported rTRE and the feature-distance estimate, with bootstrap sampling.

**Architecture:** A self-contained `benchmarks/registration_eval/` package. It drives the standalone `bin/register.py` directly on challenge image pairs (tiled on/off) — no Nextflow/production edits — then loads the VALIS registrar pickle, warps the moving ground-truth landmarks via `Slide.warp_xy`, and computes TRE/rTRE/µm. Pure math + parsing are unit-tested; VALIS-dependent warping is dependency-injected so tests run without VALIS installed.

**Tech Stack:** Python 3 (numpy, pandas, pytest), vendored `valis_lib` (runtime only, not in tests), bash driver.

This is **Plan 2 of 4**. It produces per-pair/per-mode `eval_*.json` and an aggregated CSV that the Plan 3 notebook consumes. Builds on the `benchmarking` branch from Plan 1.

**Spec:** `docs/superpowers/specs/2026-06-16-benchmarking-framework-design.md` §5.

## Decisions baked in (from brainstorming)

- **Accuracy axis:** classic VALIS, `--use-tiled-registration` ON vs OFF. Both produce a registrar pickle → exact `warp_xy` TRE.
- **Three-way error per pair/mode:** (a) true landmark TRE (warp_xy), (b) VALIS `*_rTRE` from `{name}_summary.csv`, (c) feature-distance estimate from `estimate_feature_distances.py`.
- **No production edits:** drive `bin/register.py` directly (it's a standalone CLI). This also avoids the DAPI/brightfield mismatch of the full pipeline.
- **Known limitations (documented, not built):** the Nextflow-*distributed* tiling path is not landmark-warpable (no single pickle) — covered only by estimates if run; ACROBAT landmark column names are assumed from public docs and may need a one-line tweak by the user.

## File Structure

```
benchmarks/registration_eval/
  __init__.py
  landmarks.py            # pure: LandmarkPair, per_landmark_tre, image_diagonal, summarize
  adapters/
    __init__.py
    anhir.py              # ANHIR X,Y CSV -> LandmarkPair (rTRE / image-diagonal)
    acrobat.py            # ACROBAT multi-annotator CSV -> LandmarkPair(s) (µm)
  prepare_pairs.py        # challenge dir -> per-pair input dirs + pairs_manifest.csv
  run_registration.sh     # per pair x mode: run bin/register.py (+ optional feature distances)
  eval_tre.py             # load pickle -> warp_xy -> TRE; merge VALIS + feature estimates -> eval_*.json
  sampling.py             # bootstrap CIs + paired tiled-vs-untiled test
  aggregate_eval.py       # glob eval_*.json -> registration_eval.csv (+ MMrTRE/AMrTRE)
  README.md               # data access (gating, brightfield), run steps, limitations
benchmarks/tests/
  test_landmarks.py
  test_adapters.py
  test_eval_tre.py        # uses an injected fake registrar (no VALIS needed)
  test_sampling.py
  test_aggregate_eval.py
benchmarks/tests/fixtures/
  anhir_source.csv, anhir_target.csv, acrobat_landmarks.csv   # tiny synthetic
```

---

## Task 1: Landmark model + TRE/rTRE/µm math (TDD)

**Files:** Create `benchmarks/registration_eval/__init__.py`, `benchmarks/registration_eval/landmarks.py`; Test `benchmarks/tests/test_landmarks.py`.

- [ ] **Step 1: Write the failing tests**

Create `benchmarks/tests/test_landmarks.py`:

```python
import numpy as np
import pytest
from benchmarks.registration_eval.landmarks import (
    LandmarkPair, per_landmark_tre, image_diagonal, summarize,
)


def test_per_landmark_tre_euclidean():
    warped = np.array([[0.0, 0.0], [3.0, 0.0]])
    target = np.array([[0.0, 0.0], [0.0, 4.0]])
    tre = per_landmark_tre(warped, target)
    np.testing.assert_allclose(tre, [0.0, 5.0])


def test_per_landmark_tre_shape_mismatch_raises():
    with pytest.raises(ValueError):
        per_landmark_tre(np.zeros((3, 2)), np.zeros((2, 2)))


def test_image_diagonal():
    assert image_diagonal(3, 4) == pytest.approx(5.0)


def test_summarize_px_and_rtre():
    tre = np.array([0.0, 10.0])  # median 5, mean 5
    s = summarize(tre, diagonal=100.0)
    assert s["n"] == 2
    assert s["median_px"] == pytest.approx(5.0)
    assert s["mean_px"] == pytest.approx(5.0)
    assert s["median_rtre"] == pytest.approx(0.05)
    assert s["p90_px"] == pytest.approx(9.0)  # numpy linear interp on [0,10]


def test_summarize_microns_when_pixel_size_given():
    tre = np.array([2.0, 2.0])
    s = summarize(tre, diagonal=100.0, pixel_size_um=0.5)
    assert s["median_um"] == pytest.approx(1.0)
    assert s["p90_um"] == pytest.approx(1.0)


def test_summarize_omits_microns_without_pixel_size():
    s = summarize(np.array([1.0]), diagonal=10.0)
    assert "median_um" not in s


def test_landmarkpair_holds_arrays():
    lp = LandmarkPair(moving_xy=np.zeros((2, 2)), target_xy=np.ones((2, 2)), pair_id="p1")
    assert lp.pair_id == "p1" and lp.moving_xy.shape == (2, 2)
```

- [ ] **Step 2: Run, verify FAIL**

Run: `python -m pytest benchmarks/tests/test_landmarks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'benchmarks.registration_eval'`.

- [ ] **Step 3: Implement**

Create `benchmarks/registration_eval/__init__.py` (empty).

Create `benchmarks/registration_eval/landmarks.py`:

```python
"""Landmark model and target-registration-error (TRE) math.

Pure and dependency-free (numpy only) so it is fully unit-testable.
TRE conventions follow ANHIR (rTRE = TRE / image diagonal) and ACROBAT (µm).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class LandmarkPair:
    """Matched landmarks for one image pair, in pixel coordinates.

    moving_xy: (N, 2) points in the MOVING image frame (to be warped).
    target_xy: (N, 2) corresponding points in the TARGET/reference frame.
    """
    moving_xy: np.ndarray
    target_xy: np.ndarray
    pair_id: str = ""
    meta: dict = field(default_factory=dict)


def per_landmark_tre(warped_xy, target_xy) -> np.ndarray:
    """Euclidean distance (pixels) between each warped point and its target."""
    a = np.asarray(warped_xy, dtype=float)
    b = np.asarray(target_xy, dtype=float)
    if a.shape != b.shape or a.ndim != 2 or a.shape[1] != 2:
        raise ValueError(f"expected matching (N,2) arrays, got {a.shape} and {b.shape}")
    return np.sqrt(((a - b) ** 2).sum(axis=1))


def image_diagonal(width: int, height: int) -> float:
    """Image diagonal in pixels (rTRE denominator)."""
    return float(np.hypot(width, height))


def summarize(tre_px, diagonal: float, pixel_size_um: float | None = None) -> dict:
    """Reduce per-landmark TRE to summary stats. rTRE = TRE / diagonal."""
    tre = np.asarray(tre_px, dtype=float)
    diag = float(diagonal)
    out = {
        "n": int(tre.size),
        "median_px": float(np.median(tre)),
        "mean_px": float(np.mean(tre)),
        "p90_px": float(np.percentile(tre, 90)),
        "median_rtre": float(np.median(tre) / diag),
        "mean_rtre": float(np.mean(tre / diag)),
    }
    if pixel_size_um is not None:
        out["median_um"] = float(np.median(tre) * pixel_size_um)
        out["p90_um"] = float(np.percentile(tre, 90) * pixel_size_um)
    return out
```

- [ ] **Step 4: Run, verify PASS**

Run: `python -m pytest benchmarks/tests/test_landmarks.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/registration_eval/__init__.py benchmarks/registration_eval/landmarks.py benchmarks/tests/test_landmarks.py
git commit -m ":sparkles: registration-eval landmark model + TRE/rTRE/um math" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: ANHIR + ACROBAT landmark adapters (TDD)

**Files:** Create `benchmarks/registration_eval/adapters/__init__.py`, `adapters/anhir.py`, `adapters/acrobat.py`; tiny fixtures; Test `benchmarks/tests/test_adapters.py`.

- [ ] **Step 1: Create tiny fixtures**

Create `benchmarks/tests/fixtures/anhir_source.csv`:

```csv
,X,Y
0,10.0,20.0
1,30.0,40.0
```

Create `benchmarks/tests/fixtures/anhir_target.csv`:

```csv
,X,Y
0,11.0,19.0
1,29.0,41.0
```

Create `benchmarks/tests/fixtures/acrobat_landmarks.csv` (one pair, two landmarks, two annotators averaged):

```csv
pair_id,point_id,annotator,mpp,x_ihc,y_ihc,x_he,y_he
pairA,0,ann1,1.0,100.0,200.0,105.0,195.0
pairA,0,ann2,1.0,102.0,198.0,107.0,193.0
pairA,1,ann1,1.0,300.0,400.0,295.0,405.0
pairA,1,ann2,1.0,302.0,398.0,297.0,403.0
```

- [ ] **Step 2: Write the failing tests**

Create `benchmarks/tests/test_adapters.py`:

```python
from pathlib import Path

import numpy as np

from benchmarks.registration_eval.adapters import anhir, acrobat

FIX = Path(__file__).parent / "fixtures"


def test_anhir_load_pair_reads_xy_and_aligns():
    lp = anhir.load_pair(FIX / "anhir_source.csv", FIX / "anhir_target.csv", pair_id="a")
    assert lp.pair_id == "a"
    np.testing.assert_allclose(lp.moving_xy, [[10, 20], [30, 40]])
    np.testing.assert_allclose(lp.target_xy, [[11, 19], [29, 41]])


def test_anhir_mismatched_lengths_raise():
    import pytest
    short = FIX / "anhir_source.csv"
    # target with extra row simulated by reusing source twice is same length; instead
    # assert the guard exists by calling with a deliberately bad pair via monkeypatch-free check:
    with pytest.raises(ValueError):
        anhir._validate_aligned(np.zeros((3, 2)), np.zeros((2, 2)))


def test_acrobat_load_pairs_averages_annotators():
    pairs = acrobat.load_pairs(FIX / "acrobat_landmarks.csv")
    assert len(pairs) == 1
    lp = pairs[0]
    assert lp.pair_id == "pairA"
    # annotator-averaged moving (IHC) point 0: (101, 199); point 1: (301, 399)
    np.testing.assert_allclose(lp.moving_xy, [[101, 199], [301, 399]])
    np.testing.assert_allclose(lp.target_xy, [[106, 194], [296, 404]])
    assert lp.meta["mpp"] == 1.0
```

- [ ] **Step 3: Run, verify FAIL**

Run: `python -m pytest benchmarks/tests/test_adapters.py -v`
Expected: FAIL — import error for `adapters`.

- [ ] **Step 4: Implement adapters**

Create `benchmarks/registration_eval/adapters/__init__.py` (empty).

Create `benchmarks/registration_eval/adapters/anhir.py`:

```python
"""ANHIR landmark adapter.

ANHIR ships one CSV per image with columns `,X,Y` (unnamed index, X, Y) in
pixel coordinates. A pair = source CSV + target CSV with row-aligned landmarks.
Error is reported as rTRE = TRE / image diagonal (handled in landmarks.summarize).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ..landmarks import LandmarkPair


def load_landmarks(csv_path) -> np.ndarray:
    df = pd.read_csv(csv_path)
    return df[["X", "Y"]].to_numpy(dtype=float)


def _validate_aligned(moving: np.ndarray, target: np.ndarray) -> None:
    if moving.shape != target.shape:
        raise ValueError(f"landmark count mismatch: {moving.shape} vs {target.shape}")


def load_pair(source_csv, target_csv, pair_id: str = "") -> LandmarkPair:
    moving = load_landmarks(source_csv)
    target = load_landmarks(target_csv)
    _validate_aligned(moving, target)
    return LandmarkPair(moving_xy=moving, target_xy=target,
                        pair_id=pair_id or Path(source_csv).stem)
```

Create `benchmarks/registration_eval/adapters/acrobat.py`:

```python
"""ACROBAT landmark adapter.

ACROBAT provides multi-annotator landmark pairs (moving = IHC, target = H&E),
with error reported in µm via microns-per-pixel (mpp). Multiple annotators per
landmark are averaged.

NOTE: column names below follow the public challenge description and may need a
one-line tweak to match your downloaded CSV (see COLS). pair grouping is by
`pair_id`, landmark identity by `point_id`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..landmarks import LandmarkPair

COLS = dict(pair="pair_id", point="point_id", mpp="mpp",
            mov_x="x_ihc", mov_y="y_ihc", tgt_x="x_he", tgt_y="y_he")


def load_pairs(csv_path) -> list[LandmarkPair]:
    df = pd.read_csv(csv_path)
    pairs: list[LandmarkPair] = []
    for pid, g in df.groupby(COLS["pair"], sort=True):
        agg = g.groupby(COLS["point"], sort=True).mean(numeric_only=True)
        moving = agg[[COLS["mov_x"], COLS["mov_y"]]].to_numpy(dtype=float)
        target = agg[[COLS["tgt_x"], COLS["tgt_y"]]].to_numpy(dtype=float)
        mpp = float(agg[COLS["mpp"]].iloc[0])
        pairs.append(LandmarkPair(moving_xy=moving, target_xy=target,
                                  pair_id=str(pid), meta={"mpp": mpp}))
    return pairs
```

- [ ] **Step 5: Run, verify PASS**

Run: `python -m pytest benchmarks/tests/test_adapters.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add benchmarks/registration_eval/adapters benchmarks/tests/test_adapters.py benchmarks/tests/fixtures
git commit -m ":sparkles: ANHIR + ACROBAT landmark adapters" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: TRE evaluation with injected registrar (TDD)

**Files:** Create `benchmarks/registration_eval/eval_tre.py`; Test `benchmarks/tests/test_eval_tre.py`.

The VALIS-dependent warp is isolated behind `warp_moving_landmarks` + a `loader` callable so tests inject a fake registrar (no VALIS install needed).

- [ ] **Step 1: Write the failing tests**

Create `benchmarks/tests/test_eval_tre.py`:

```python
import json

import numpy as np

from benchmarks.registration_eval.landmarks import LandmarkPair
from benchmarks.registration_eval import eval_tre


class _FakeSlide:
    def warp_xy(self, xy, non_rigid=True):
        # pretend perfect registration: identity warp
        return np.asarray(xy, dtype=float)


class _FakeRegistrar:
    def __init__(self):
        self.slide_dict = {"moving": _FakeSlide()}


def test_warp_moving_landmarks_uses_named_slide():
    reg = _FakeRegistrar()
    out = eval_tre.warp_moving_landmarks(reg, "moving", np.array([[1.0, 2.0]]))
    np.testing.assert_allclose(out, [[1.0, 2.0]])


def test_evaluate_pair_perfect_registration_gives_known_tre():
    # target offset by (3,4) from moving; identity warp -> TRE 5 everywhere
    pair = LandmarkPair(
        moving_xy=np.array([[0.0, 0.0], [10.0, 10.0]]),
        target_xy=np.array([[3.0, 4.0], [13.0, 14.0]]),
        pair_id="p1",
    )
    summary, tre = eval_tre.evaluate_pair(
        pair, pickle_path="ignored", slide_name="moving",
        width=100, height=0, pixel_size_um=2.0,
        loader=lambda _p: _FakeRegistrar(),
    )
    np.testing.assert_allclose(tre, [5.0, 5.0])
    assert summary["median_px"] == 5.0
    assert summary["median_rtre"] == 5.0 / 100.0
    assert summary["median_um"] == 10.0


def test_build_eval_record_merges_three_estimates():
    rec = eval_tre.build_eval_record(
        pair_id="p1", mode="tiled",
        tre_summary={"median_px": 5.0, "median_rtre": 0.05},
        valis_rtre=0.06, feature_estimate={"median": 4.2},
    )
    assert rec["pair_id"] == "p1" and rec["mode"] == "tiled"
    assert rec["true_tre"]["median_px"] == 5.0
    assert rec["valis_rtre"] == 0.06
    assert rec["feature_estimate"]["median"] == 4.2


def test_read_valis_rtre_picks_non_rigid_then_rigid(tmp_path):
    p = tmp_path / "s_summary.csv"
    p.write_text("rigid_rTRE,non_rigid_rTRE\n0.09,0.04\n")
    assert eval_tre.read_valis_rtre(p) == 0.04
    p.write_text("rigid_rTRE,non_rigid_rTRE\n0.09,\n")
    assert eval_tre.read_valis_rtre(p) == 0.09


def test_read_feature_estimate_extracts_after_registration(tmp_path):
    p = tmp_path / "fd.json"
    p.write_text(json.dumps({"after_registration": {"feature_distances": {"median": 3.3}}}))
    assert eval_tre.read_feature_estimate(p) == {"median": 3.3}
```

- [ ] **Step 2: Run, verify FAIL**

Run: `python -m pytest benchmarks/tests/test_eval_tre.py -v`
Expected: FAIL — import error for `eval_tre`.

- [ ] **Step 3: Implement**

Create `benchmarks/registration_eval/eval_tre.py`:

```python
"""Evaluate registration accuracy for one image pair / mode.

Loads a VALIS registrar pickle, warps the moving ground-truth landmarks into the
target frame, and merges three estimates: true landmark TRE, VALIS self-rTRE,
and the feature-distance estimate. VALIS import is lazy + injectable so the pure
logic is testable without VALIS.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .landmarks import LandmarkPair, image_diagonal, per_landmark_tre, summarize


def default_loader(pickle_path):
    from valis_lib import registration  # lazy: only needed at runtime
    return registration.load_registrar(str(pickle_path))


def warp_moving_landmarks(registrar, slide_name: str, moving_xy, non_rigid: bool = True) -> np.ndarray:
    slide = registrar.slide_dict[slide_name]
    return np.asarray(slide.warp_xy(np.asarray(moving_xy, dtype=float), non_rigid=non_rigid), dtype=float)


def evaluate_pair(pair: LandmarkPair, pickle_path, slide_name: str, width: int, height: int,
                  pixel_size_um=None, loader=default_loader, non_rigid: bool = True):
    registrar = loader(pickle_path)
    warped = warp_moving_landmarks(registrar, slide_name, pair.moving_xy, non_rigid=non_rigid)
    tre = per_landmark_tre(warped, pair.target_xy)
    return summarize(tre, image_diagonal(width, height), pixel_size_um), tre


def read_valis_rtre(summary_csv):
    """VALIS self-reported rTRE: prefer non_rigid_rTRE, fall back to rigid_rTRE."""
    df = pd.read_csv(summary_csv)
    for col in ("non_rigid_rTRE", "rigid_rTRE"):
        if col in df.columns:
            val = pd.to_numeric(df[col], errors="coerce").dropna()
            if not val.empty:
                return float(val.iloc[0])
    return None


def read_feature_estimate(feature_json):
    data = json.loads(Path(feature_json).read_text())
    return data.get("after_registration", {}).get("feature_distances")


def build_eval_record(pair_id, mode, tre_summary, valis_rtre=None, feature_estimate=None) -> dict:
    return {
        "pair_id": pair_id,
        "mode": mode,
        "true_tre": tre_summary,
        "valis_rtre": valis_rtre,
        "feature_estimate": feature_estimate,
    }


def main():
    ap = argparse.ArgumentParser(description="Evaluate registration TRE for one pair/mode.")
    ap.add_argument("--pickle", required=True, help="VALIS registrar pickle")
    ap.add_argument("--slide-name", required=True, help="moving slide name in registrar.slide_dict")
    ap.add_argument("--source-landmarks", required=True)
    ap.add_argument("--target-landmarks", required=True)
    ap.add_argument("--adapter", choices=["anhir"], default="anhir",
                    help="landmark loader (acrobat pairs are pre-split by prepare_pairs)")
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--height", type=int, required=True)
    ap.add_argument("--pixel-size-um", type=float, default=None)
    ap.add_argument("--valis-summary", default=None)
    ap.add_argument("--feature-json", default=None)
    ap.add_argument("--mode", required=True, choices=["tiled", "untiled"])
    ap.add_argument("--pair-id", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    from .adapters import anhir
    pair = anhir.load_pair(a.source_landmarks, a.target_landmarks, pair_id=a.pair_id)
    tre_summary, _ = evaluate_pair(pair, a.pickle, a.slide_name, a.width, a.height, a.pixel_size_um)
    record = build_eval_record(
        a.pair_id, a.mode, tre_summary,
        valis_rtre=read_valis_rtre(a.valis_summary) if a.valis_summary else None,
        feature_estimate=read_feature_estimate(a.feature_json) if a.feature_json else None,
    )
    Path(a.out).write_text(json.dumps(record, indent=2))
    print(f"Wrote {a.out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run, verify PASS**

Run: `python -m pytest benchmarks/tests/test_eval_tre.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/registration_eval/eval_tre.py benchmarks/tests/test_eval_tre.py
git commit -m ":sparkles: TRE evaluation merging true-TRE, VALIS rTRE, feature estimate" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Bootstrap sampling + paired comparison (TDD)

**Files:** Create `benchmarks/registration_eval/sampling.py`; Test `benchmarks/tests/test_sampling.py`.

- [ ] **Step 1: Write the failing tests**

Create `benchmarks/tests/test_sampling.py`:

```python
import numpy as np

from benchmarks.registration_eval import sampling


def test_bootstrap_ci_is_deterministic_and_brackets_point():
    vals = np.arange(100.0)
    lo, point, hi = sampling.bootstrap_ci(vals, n_boot=500, ci=95, seed=0)
    lo2, point2, hi2 = sampling.bootstrap_ci(vals, n_boot=500, ci=95, seed=0)
    assert (lo, point, hi) == (lo2, point2, hi2)  # deterministic
    assert lo <= point <= hi
    assert point == np.median(vals)


def test_paired_diff_test_detects_positive_shift():
    rng = np.random.default_rng(0)
    a = rng.normal(10.0, 1.0, size=50)   # tiled (worse, higher TRE)
    b = a - 2.0                          # untiled consistently 2 lower
    res = sampling.paired_diff_test(a, b, n_boot=500, seed=0)
    assert res["median_diff"] > 0
    assert res["ci_low"] > 0             # CI excludes zero -> significant
    assert res["frac_positive"] > 0.95


def test_paired_diff_test_requires_equal_length():
    import pytest
    with pytest.raises(ValueError):
        sampling.paired_diff_test(np.zeros(3), np.zeros(4))
```

- [ ] **Step 2: Run, verify FAIL**

Run: `python -m pytest benchmarks/tests/test_sampling.py -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement**

Create `benchmarks/registration_eval/sampling.py`:

```python
"""Bootstrap confidence intervals and paired tiled-vs-untiled comparison."""
from __future__ import annotations

import numpy as np


def bootstrap_ci(values, n_boot: int = 1000, ci: float = 95, seed: int = 0, stat=np.median):
    """Return (ci_low, point_estimate, ci_high) for `stat` over bootstrap resamples."""
    v = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    boot = np.array([stat(rng.choice(v, size=v.size, replace=True)) for _ in range(n_boot)])
    half = (100 - ci) / 2
    return (float(np.percentile(boot, half)), float(stat(v)), float(np.percentile(boot, 100 - half)))


def paired_diff_test(a, b, n_boot: int = 1000, ci: float = 95, seed: int = 0) -> dict:
    """Bootstrap the median paired difference (a - b). CI excluding 0 => significant.

    `frac_positive` is the fraction of bootstrap medians > 0 (a one-sided mass).
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.shape != b.shape:
        raise ValueError(f"paired arrays must match: {a.shape} vs {b.shape}")
    diff = a - b
    rng = np.random.default_rng(seed)
    boot = np.array([np.median(rng.choice(diff, size=diff.size, replace=True)) for _ in range(n_boot)])
    half = (100 - ci) / 2
    return {
        "median_diff": float(np.median(diff)),
        "ci_low": float(np.percentile(boot, half)),
        "ci_high": float(np.percentile(boot, 100 - half)),
        "frac_positive": float(np.mean(boot > 0)),
        "n": int(diff.size),
    }
```

- [ ] **Step 4: Run, verify PASS**

Run: `python -m pytest benchmarks/tests/test_sampling.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/registration_eval/sampling.py benchmarks/tests/test_sampling.py
git commit -m ":sparkles: bootstrap CIs + paired tiled-vs-untiled comparison" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Aggregate eval records → tidy CSV (TDD)

**Files:** Create `benchmarks/registration_eval/aggregate_eval.py`; Test `benchmarks/tests/test_aggregate_eval.py`.

- [ ] **Step 1: Write the failing tests**

Create `benchmarks/tests/test_aggregate_eval.py`:

```python
import json

from benchmarks.registration_eval import aggregate_eval


def _write(p, rec):
    p.write_text(json.dumps(rec))


def test_aggregate_flattens_records_and_computes_anhir_aggregates(tmp_path):
    _write(tmp_path / "e1.json", {
        "pair_id": "p1", "mode": "tiled",
        "true_tre": {"median_px": 4.0, "median_rtre": 0.04, "p90_px": 6.0},
        "valis_rtre": 0.05, "feature_estimate": {"median": 3.8},
    })
    _write(tmp_path / "e2.json", {
        "pair_id": "p2", "mode": "tiled",
        "true_tre": {"median_px": 6.0, "median_rtre": 0.06, "p90_px": 8.0},
        "valis_rtre": 0.07, "feature_estimate": {"median": 5.0},
    })
    df, agg = aggregate_eval.aggregate(tmp_path)
    assert set(df.columns) >= {"pair_id", "mode", "true_median_px", "true_median_rtre",
                               "valis_rtre", "feature_median"}
    assert len(df) == 2
    # MMrTRE = median of per-pair median rTRE; AMrTRE = mean of them
    row = agg[agg["mode"] == "tiled"].iloc[0]
    assert row["MMrTRE"] == 0.05   # median(0.04, 0.06)
    assert row["AMrTRE"] == 0.05   # mean(0.04, 0.06)
```

- [ ] **Step 2: Run, verify FAIL**

Run: `python -m pytest benchmarks/tests/test_aggregate_eval.py -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement**

Create `benchmarks/registration_eval/aggregate_eval.py`:

```python
"""Aggregate per-pair/mode eval_*.json into a tidy CSV for the analysis notebook."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def _flatten(rec: dict) -> dict:
    tre = rec.get("true_tre") or {}
    feat = rec.get("feature_estimate") or {}
    return {
        "pair_id": rec.get("pair_id"),
        "mode": rec.get("mode"),
        "true_median_px": tre.get("median_px"),
        "true_mean_px": tre.get("mean_px"),
        "true_p90_px": tre.get("p90_px"),
        "true_median_rtre": tre.get("median_rtre"),
        "true_median_um": tre.get("median_um"),
        "true_p90_um": tre.get("p90_um"),
        "valis_rtre": rec.get("valis_rtre"),
        "feature_median": feat.get("median"),
    }


def aggregate(eval_dir):
    """Return (per_pair_df, per_mode_agg_df). per_mode adds ANHIR MMrTRE/AMrTRE."""
    rows = [_flatten(json.loads(p.read_text())) for p in sorted(Path(eval_dir).glob("*.json"))]
    df = pd.DataFrame(rows)
    agg = (
        df.groupby("mode")["true_median_rtre"]
        .agg(MMrTRE="median", AMrTRE="mean")
        .reset_index()
    )
    return df, agg


def main():
    ap = argparse.ArgumentParser(description="Aggregate registration eval JSONs.")
    ap.add_argument("--eval-dir", required=True)
    ap.add_argument("--out", required=True, help="output CSV (per-pair)")
    ap.add_argument("--agg-out", default=None, help="optional per-mode aggregate CSV")
    a = ap.parse_args()
    df, agg = aggregate(a.eval_dir)
    df.to_csv(a.out, index=False)
    if a.agg_out:
        agg.to_csv(a.agg_out, index=False)
    print(f"Wrote {len(df)} rows to {a.out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run, verify PASS**

Run: `python -m pytest benchmarks/tests/test_aggregate_eval.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/registration_eval/aggregate_eval.py benchmarks/tests/test_aggregate_eval.py
git commit -m ":sparkles: aggregate registration eval records to tidy CSV" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: prepare_pairs.py — challenge dir → input dirs + manifest

**Files:** Create `benchmarks/registration_eval/prepare_pairs.py`.

This is a CLI that organizes a downloaded challenge dataset into per-pair input directories (each holding the reference + moving image) for `register.py`, and writes `pairs_manifest.csv` (pair_id, ref_image, moving_image, source_landmarks, target_landmarks, width, height, pixel_size_um). It is config/IO glue; unit-test the one pure helper.

- [ ] **Step 1: Write a failing test for the pure helper**

Append to `benchmarks/tests/test_adapters.py` (it already imports adapters; add at bottom):

```python
def test_pairs_manifest_row_builds_expected_fields():
    from benchmarks.registration_eval.prepare_pairs import build_manifest_row
    row = build_manifest_row(
        pair_id="P1", ref_image="/d/ref.tif", moving_image="/d/mov.tif",
        source_landmarks="/d/mov.csv", target_landmarks="/d/ref.csv",
        width=1000, height=800, pixel_size_um=0.5,
    )
    assert row == {
        "pair_id": "P1", "ref_image": "/d/ref.tif", "moving_image": "/d/mov.tif",
        "source_landmarks": "/d/mov.csv", "target_landmarks": "/d/ref.csv",
        "width": 1000, "height": 800, "pixel_size_um": 0.5,
    }
```

- [ ] **Step 2: Run, verify FAIL**

Run: `python -m pytest benchmarks/tests/test_adapters.py::test_pairs_manifest_row_builds_expected_fields -v`
Expected: FAIL — `ImportError: cannot import name 'build_manifest_row'`.

- [ ] **Step 3: Implement**

Create `benchmarks/registration_eval/prepare_pairs.py`:

```python
"""Organize a downloaded ANHIR/ACROBAT dataset into per-pair input dirs for register.py.

For each pair, creates <out>/<pair_id>/input/ containing symlinks to the reference
and moving images (register.py reads a directory), and writes pairs_manifest.csv.
Landmark files are referenced (not moved) for the evaluator.

Data is account-gated for both challenges — see README.md. This script does NOT
download; point --data-dir at your local copy.
"""
from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path


def build_manifest_row(pair_id, ref_image, moving_image, source_landmarks,
                       target_landmarks, width, height, pixel_size_um) -> dict:
    return {
        "pair_id": pair_id,
        "ref_image": str(ref_image),
        "moving_image": str(moving_image),
        "source_landmarks": str(source_landmarks),
        "target_landmarks": str(target_landmarks),
        "width": int(width),
        "height": int(height),
        "pixel_size_um": pixel_size_um,
    }


def _link_input_dir(out_root: Path, pair_id: str, ref_image: Path, moving_image: Path) -> Path:
    d = out_root / pair_id / "input"
    d.mkdir(parents=True, exist_ok=True)
    for img in (ref_image, moving_image):
        dst = d / Path(img).name
        if not dst.exists():
            os.symlink(os.path.abspath(img), dst)
    return d


def write_manifest(rows, out_csv: Path) -> None:
    fields = ["pair_id", "ref_image", "moving_image", "source_landmarks",
              "target_landmarks", "width", "height", "pixel_size_um"]
    with open(out_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description="Prepare challenge pairs for registration eval.")
    ap.add_argument("--pairs-csv", required=True,
                    help="user-provided CSV describing pairs: pair_id,ref_image,moving_image,"
                         "source_landmarks,target_landmarks,width,height,pixel_size_um")
    ap.add_argument("--out", required=True, help="output root for per-pair input dirs + manifest")
    a = ap.parse_args()

    out_root = Path(a.out)
    out_root.mkdir(parents=True, exist_ok=True)
    rows = []
    with open(a.pairs_csv) as fh:
        for r in csv.DictReader(fh):
            _link_input_dir(out_root, r["pair_id"], Path(r["ref_image"]), Path(r["moving_image"]))
            rows.append(build_manifest_row(
                r["pair_id"], r["ref_image"], r["moving_image"],
                r["source_landmarks"], r["target_landmarks"],
                r["width"], r["height"], float(r["pixel_size_um"]) if r.get("pixel_size_um") else None,
            ))
    write_manifest(rows, out_root / "pairs_manifest.csv")
    print(f"Prepared {len(rows)} pairs under {out_root}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run, verify PASS**

Run: `python -m pytest benchmarks/tests/test_adapters.py -v`
Expected: all pass (4 total now).

- [ ] **Step 5: Commit**

```bash
git add benchmarks/registration_eval/prepare_pairs.py benchmarks/tests/test_adapters.py
git commit -m ":sparkles: prepare_pairs - challenge dataset to register.py input dirs + manifest" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: run_registration.sh driver + README

**Files:** Create `benchmarks/registration_eval/run_registration.sh`, `benchmarks/registration_eval/README.md`.

- [ ] **Step 1: Write the driver**

Create `benchmarks/registration_eval/run_registration.sh`:

```bash
#!/usr/bin/env bash
# Register each pair twice (tiled on/off) with the standalone register.py, then
# evaluate TRE. Run where the VALIS environment / container is available.
# Usage: run_registration.sh <pairs_manifest.csv> <prepared_root> <results_root>
set -euo pipefail

MANIFEST="${1:?pairs_manifest.csv}"
PREPARED="${2:?prepared root (from prepare_pairs.py)}"
RESULTS="${3:?results root}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

mkdir -p "$RESULTS"
header=$(head -n1 "$MANIFEST"); IFS=',' read -r -a cols <<< "$header"
col() { local k="$1"; local i; for i in "${!cols[@]}"; do [ "${cols[$i]}" = "$k" ] && { echo "$i"; return; }; done; }
ci_pair=$(col pair_id); ci_mov=$(col moving_image); ci_ref=$(col ref_image)
ci_sl=$(col source_landmarks); ci_tl=$(col target_landmarks)
ci_w=$(col width); ci_h=$(col height); ci_ps=$(col pixel_size_um)

tail -n +2 "$MANIFEST" | while IFS=',' read -r -a v; do
  pair="${v[$ci_pair]}"; input_dir="$PREPARED/$pair/input"
  ref_name="$(basename "${v[$ci_ref]}")"; mov_name="$(basename "${v[$ci_mov]}")"
  mov_stem="${mov_name%%.*}"

  for mode in tiled untiled; do
    tiled_flag=""; [ "$mode" = "tiled" ] && tiled_flag="--use-tiled-registration"
    out_dir="$RESULTS/$pair/$mode"; mkdir -p "$out_dir"
    echo ">>> $pair [$mode]"
    python "$REPO_ROOT/bin/register.py" \
      --input-dir "$input_dir" --out "$out_dir/registered_slides" \
      --reference "$ref_name" $tiled_flag || { echo "REG FAILED: $pair $mode" >&2; continue; }

    pickle=$(find "$out_dir" -name "*_registrar.pickle" | head -1)
    summary=$(find "$out_dir" -name "*_summary.csv" | head -1)
    reg_img=$(find "$out_dir/registered_slides" -name "*_registered.ome.tiff" | head -1)

    # optional feature-distance estimate on the registered output
    fjson="$out_dir/feature_distances.json"
    python "$REPO_ROOT/bin/estimate_feature_distances.py" \
      --reference "$input_dir/$ref_name" --moving "$input_dir/$mov_name" \
      --registered "$reg_img" --output-prefix "$out_dir/fd" 2>/dev/null || fjson=""

    python -m benchmarks.registration_eval.eval_tre \
      --pickle "$pickle" --slide-name "$mov_stem" \
      --source-landmarks "${v[$ci_sl]}" --target-landmarks "${v[$ci_tl]}" \
      --width "${v[$ci_w]}" --height "${v[$ci_h]}" \
      ${v[$ci_ps]:+--pixel-size-um "${v[$ci_ps]}"} \
      ${summary:+--valis-summary "$summary"} \
      ${fjson:+--feature-json "$out_dir/fd_feature_distances.json"} \
      --mode "$mode" --pair-id "$pair" --out "$RESULTS/eval_${pair}_${mode}.json"
  done
done
echo "Done. Aggregate with: python -m benchmarks.registration_eval.aggregate_eval --eval-dir $RESULTS --out reg_eval.csv --agg-out reg_eval_agg.csv"
```

- [ ] **Step 2: chmod + syntax check**

Run: `chmod +x benchmarks/registration_eval/run_registration.sh && bash -n benchmarks/registration_eval/run_registration.sh && echo OK`
Expected: `OK`.

- [ ] **Step 3: Write the README**

Create `benchmarks/registration_eval/README.md`:

```markdown
# Registration-Accuracy Evaluation

Compares Mirage's VALIS registration with in-process tiling **ON vs OFF** against
landmark ground truth, reporting three error views per pair/mode:
true landmark **TRE / rTRE / µm**, VALIS's self-reported **rTRE**, and the
**feature-distance** estimate. See spec §5.

## Data access (you must download — both are gated)

- **ANHIR** — create a grand-challenge.org account, join the challenge, accept the
  CC-BY-NC-SA licence, download. Landmarks ship as `,X,Y` CSVs alongside multi-scale images.
  <https://anhir.grand-challenge.org/Data/>
- **ACROBAT** — WSIs are open on the Swedish National Data Service, but **landmark
  annotations are behind the challenge account**. <https://acrobat.grand-challenge.org/>

> The evaluator is format-driven and download-independent. The ACROBAT adapter's
> column names (`benchmarks/registration_eval/adapters/acrobat.py:COLS`) follow the
> public docs — adjust them if your downloaded CSV differs.

## Run

1. Describe your pairs in a CSV (`pair_id,ref_image,moving_image,source_landmarks,target_landmarks,width,height,pixel_size_um`), then:

       python -m benchmarks.registration_eval.prepare_pairs --pairs-csv pairs.csv --out reg_prepared

2. Register (tiled/untiled) + evaluate (run where the VALIS env is available):

       benchmarks/registration_eval/run_registration.sh reg_prepared/pairs_manifest.csv reg_prepared reg_results

3. Aggregate for the notebook (Plan 3):

       python -m benchmarks.registration_eval.aggregate_eval --eval-dir reg_results --out reg_eval.csv --agg-out reg_eval_agg.csv

## Limitations

- **Brightfield input:** ANHIR/ACROBAT are H&E/IHC. We drive `bin/register.py`
  directly (not the full Mirage pipeline), so the DAPI requirement and BaSiC
  preprocessing do not apply. The registration is the same VALIS used by the pipeline.
- **Nextflow-distributed tiling** (`reg_distributed_tiling`) produces no single VALIS
  registrar pickle, so true landmark TRE is not available for it. Compare that path
  with the feature-distance estimate on its registered output if needed.
- **`requires bash 4+`** — `run_registration.sh` uses indexed-array column lookup
  compatible with bash 3, but on macOS prefer a homebrew bash.
```

- [ ] **Step 4: Commit**

```bash
git add benchmarks/registration_eval/run_registration.sh benchmarks/registration_eval/README.md
git commit -m ":sparkles: registration eval driver + README (data access, limitations)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Full suite verification

**Files:** none (verification).

- [ ] **Step 1: Run the whole benchmark suite**

Run: `python -m pytest benchmarks/tests -q`
Expected: all pass (Plan 1's 18 + Plan 2's 7+3+6+3+1+1 = 21 new → 39 total). Report the exact count.

- [ ] **Step 2: Confirm the existing project suite is unaffected**

Run: `python -m pytest -q tests/ --ignore=tests/testdata --ignore=tests/modules --ignore=tests/subworkflows --ignore=tests/integration 2>&1 | tail -5`
Expected: still 19 passed, 3 skipped.

- [ ] **Step 3: Syntax-check the driver**

Run: `bash -n benchmarks/registration_eval/run_registration.sh && echo OK`

---

## Self-Review

**Spec coverage (§5):**
- §5.1 ground-truth ingest (generic loader + ANHIR/ACROBAT adapters, prepare_pairs) → Tasks 2, 6. ✓
- §5.2 true TRE via VALIS warp (load_registrar → warp_xy) → Task 3. ✓
- §5.3 three-way comparison (true / VALIS rtre / feature) → Tasks 3, 5. ✓
- §5.4 register.py change → **resolved as not needed** (drive register.py directly; pickle/summary read from its data dir). Documented in plan header + README. ✓
- §5.5 sampling (bootstrap CIs, paired) → Task 4. ✓

**Placeholder scan:** none — all code is concrete.

**Type/name consistency:** `LandmarkPair(moving_xy, target_xy, pair_id, meta)` consistent across landmarks/adapters/eval_tre/tests; `summarize` keys (`median_px`, `median_rtre`, `p90_px`, `median_um`, `p90_um`) consistent between landmarks.py, eval_tre tests, and aggregate_eval `_flatten`; `evaluate_pair(..., loader=...)` injection matches the test's `loader=lambda`; `read_valis_rtre` column names (`non_rigid_rTRE`/`rigid_rTRE`) match the VALIS summary_df columns from the spec; eval JSON shape (`true_tre`/`valis_rtre`/`feature_estimate`) consistent between `build_eval_record` and `aggregate_eval._flatten`.

---

## Remaining plans

- **Plan 3 — Analysis notebook + optimal config:** consumes Plan 1's `trace.txt`/`input_sizes.csv`/`matrix_manifest.csv` and Plan 2's `reg_eval.csv`. Builds `analysis/lib/{parse,regress,sampling,plotting}.py`, `benchmark_analysis.ipynb` (5 sections), `emit_config.py` → `conf/modules.optimized.config`.
- **Plan 4 — Docs:** `docs/benchmarks.md` + nav entry, embed exported figures.
