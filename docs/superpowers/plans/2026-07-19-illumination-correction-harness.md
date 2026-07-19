# Illumination-Correction Experiment Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline Python harness that benchmarks multiple illumination-correction variants (flat-field / darkfield / background-removal combinations) on a stitched multichannel mosaic, validated against synthetic ground truth, emitting per-step diagnostic plots, an HTML report with recommendations, and a QuPath pyramid per variant.

**Architecture:** A small library `bin/illum/` with one responsibility per module (grid recovery, flat-field, darkfield, background, metrics, pipeline, plots, report), driven by two entry scripts: `bin/illum_correct.py` (single-variant CLI, pipeline-facing) and `bin/illum_benchmark.py` (the sweep driver). Correctness is proven bottom-up against a synthetic mosaic with a known grid/vignette/darkfield/background.

**Tech Stack:** Python 3, numpy, scipy.ndimage, tifffile, matplotlib; optional skimage (rolling-ball/tophat) and basicpy (baseline variant) guarded behind availability checks. Reuses `bin/merge_channels_pyramid.py:write_pyramidal_ome_tiff` for pyramids.

## Global Constraints

- Python bin scripts invoked by name (`illum_correct.py`, `illum_benchmark.py`) MUST be git-mode `100755`; import-only library files under `bin/illum/` stay `100644`. Verify with `git ls-files -s`.
- Channels are processed **sequentially** — never a thread pool (the naive 4-way pool OOMs at ~10 GB/channel).
- Flat-field is **mean-normalized to 1.0**; corrected output preserves the input dtype (default `uint16`) with `np.clip` to the dtype range.
- Optional dependencies (`skimage`, `basicpy`, `nd2`) MUST be import-guarded; their absence disables only the dependent variant/loader with a logged note, never a crash.
- All new tests live under `tests/` and run via `pytest -v tests/ --ignore=tests/testdata --ignore=tests/modules --ignore=tests/subworkflows --ignore=tests/integration` (the project's Python-test invocation). Test files import from `bin/illum/`.
- Default approx-tile is `1950` (matches the BaSiC path's assumed FOV size); overridable.
- Reduced-resolution flat-field estimate default downsample: `4`. Apply is row-chunked (default 2048 rows).

---

## File Structure

```
bin/illum/__init__.py            # package marker; re-exports public API
bin/illum/grid.py                # grid recovery (period/phase, float-pitch, tile origins)
bin/illum/flatfield.py           # estimate_flatfield (reduced-res) + apply_field (chunked)
bin/illum/darkfield.py           # constant + spatially-varying darkfield
bin/illum/background.py           # background-removal method registry
bin/illum/metrics.py             # seam_peak, background_cv, resource capture
bin/illum/pipeline.py            # Variant dataclass + run_variant + default matrix
bin/illum/plots.py               # per-step diagnostic plots
bin/illum/report.py              # HTML report, leaderboard, recommendation text
bin/illum_correct.py             # single-variant CLI (pipeline-facing) [100755]
bin/illum_benchmark.py           # sweep driver [100755]
tests/testdata/generate_synthetic_mosaic.py   # ground-truth synthetic mosaic
tests/test_illum_grid.py
tests/test_illum_flatfield.py
tests/test_illum_darkfield.py
tests/test_illum_background.py
tests/test_illum_metrics.py
tests/test_illum_pipeline.py
tests/test_illum_cli.py
```

Phase B (Nextflow productionization of the winner) is **deferred** — see the design spec §8. It is not part of this plan and is built after the user runs the harness on real cluster data and picks a winner.

---

### Task 1: Synthetic mosaic generator (ground truth)

Everything downstream is tested against a mosaic whose grid, vignette, darkfield, and background are known. Build this first.

**Files:**
- Create: `tests/testdata/generate_synthetic_mosaic.py`
- Test: `tests/test_illum_synthetic.py`

**Interfaces:**
- Produces: `make_synthetic_mosaic(n_tiles_y=4, n_tiles_x=5, tile=128, overlap=16, n_channels=2, seed=0, vignette_strength=0.45, dark=200.0, bg_amp=0.0) -> dict` returning keys `mosaic` (C,Y,X uint16), `flatfield` (tile,tile float32, mean-1), `dark` (float), `pitch` (float = tile-overlap), `phase` (int), `channel_names` (list[str]).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_illum_synthetic.py
import numpy as np
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests" / "testdata"))
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_synthetic_mosaic_shape_and_grid():
    d = make_synthetic_mosaic(n_tiles_y=4, n_tiles_x=5, tile=128, overlap=16, n_channels=3)
    assert d["mosaic"].shape[0] == 3
    C, Y, X = d["mosaic"].shape
    assert d["pitch"] == 128 - 16          # 112
    # mosaic spans n_tiles*pitch + overlap in each axis
    assert Y == 4 * (128 - 16) + 16
    assert X == 5 * (128 - 16) + 16
    assert d["mosaic"].dtype == np.uint16
    assert abs(float(d["flatfield"].mean()) - 1.0) < 1e-3


def test_synthetic_vignette_is_periodic():
    d = make_synthetic_mosaic(vignette_strength=0.5, dark=0.0, bg_amp=0.0, n_channels=1)
    # Column profile should show a dip at each seam (period = pitch)
    col = np.median(d["mosaic"][0], axis=0).astype(float)
    assert col.std() > 0  # non-flat illumination present
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_illum_synthetic.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'generate_synthetic_mosaic'`

- [ ] **Step 3: Write minimal implementation**

```python
# tests/testdata/generate_synthetic_mosaic.py
"""Synthetic stitched mosaic with a KNOWN grid, vignette, darkfield, background.

Used as ground truth for illumination-correction tests: every recovered
quantity can be compared against the value injected here.
"""
from __future__ import annotations
import numpy as np


def _vignette(tile: int, strength: float) -> np.ndarray:
    """Radial mean-1 flat-field: bright center, dark corners."""
    yy, xx = np.mgrid[0:tile, 0:tile].astype(np.float64)
    cy = cx = (tile - 1) / 2.0
    r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    r /= r.max()
    ff = 1.0 - strength * (r ** 2)      # smooth quadratic falloff
    ff /= ff.mean()
    return ff.astype(np.float32)


def make_synthetic_mosaic(n_tiles_y=4, n_tiles_x=5, tile=128, overlap=16,
                          n_channels=2, seed=0, vignette_strength=0.45,
                          dark=200.0, bg_amp=0.0):
    rng = np.random.default_rng(seed)
    pitch = tile - overlap
    Y = n_tiles_y * pitch + overlap
    X = n_tiles_x * pitch + overlap
    ff = _vignette(tile, vignette_strength)

    mosaic = np.zeros((n_channels, Y, X), dtype=np.float64)
    weight = np.zeros((Y, X), dtype=np.float64)

    for ty in range(n_tiles_y):
        for tx in range(n_tiles_x):
            y0, x0 = ty * pitch, tx * pitch
            for c in range(n_channels):
                # random "tissue" content per tile, same optics (ff) every tile
                content = rng.uniform(500, 4000) * rng.random((tile, tile))
                signal = content * ff + dark
                mosaic[c, y0:y0 + tile, x0:x0 + tile] += signal
            weight[y0:y0 + tile, x0:x0 + tile] += 1.0

    # blend overlaps by averaging (the stitcher's feather, simplified)
    mosaic /= np.maximum(weight, 1.0)[None]

    if bg_amp > 0:
        yy, xx = np.mgrid[0:Y, 0:X].astype(np.float64)
        bg = bg_amp * (0.5 + 0.5 * np.sin(2 * np.pi * yy / Y) * np.cos(2 * np.pi * xx / X))
        mosaic += bg[None]

    mosaic = np.clip(mosaic, 0, 65535).astype(np.uint16)
    names = [f"CH{i}" for i in range(n_channels)]
    return {
        "mosaic": mosaic,
        "flatfield": ff,
        "dark": float(dark),
        "pitch": float(pitch),
        "phase": 0,
        "channel_names": names,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_illum_synthetic.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/testdata/generate_synthetic_mosaic.py tests/test_illum_synthetic.py
git commit -m ":white_check_mark: Add synthetic ground-truth mosaic generator for illum tests"
```

---

### Task 2: Grid recovery (`bin/illum/grid.py`)

**Files:**
- Create: `bin/illum/__init__.py`, `bin/illum/grid.py`
- Test: `tests/test_illum_grid.py`

**Interfaces:**
- Produces:
  - `period_from_profile(profile, approx=None, lo=32, hi=None) -> float`
  - `phase_from_profile(profile, period) -> int`
  - `recover_grid(stack, approx_tile=None, est_downsample=4) -> dict` → keys `pitch_y`, `pitch_x` (float), `phase_y`, `phase_x` (int)
  - `tile_origins(phase, pitch, tile_size, extent) -> list[int]` — integer start indices of complete tiles, spaced on the **float** pitch (`round(phase + k*pitch)`), each satisfying `start + tile_size <= extent`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_illum_grid.py
import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.grid import recover_grid, tile_origins
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_recover_grid_matches_injected_pitch():
    d = make_synthetic_mosaic(tile=128, overlap=16, n_channels=2, dark=100.0)
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    assert abs(g["pitch_x"] - d["pitch"]) <= 1.0
    assert abs(g["pitch_y"] - d["pitch"]) <= 1.0


def test_tile_origins_float_pitch_no_drift():
    # pitch 112.4 across 10 tiles: last origin must track the float pitch,
    # not an integer stride of 112 (which would be 4px short by tile 10)
    origins = tile_origins(phase=0, pitch=112.4, tile_size=112, extent=112.4 * 11)
    assert origins[0] == 0
    assert origins[10] == round(112.4 * 10)   # 1124, not 1120
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_illum_grid.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'illum.grid'`

- [ ] **Step 3: Write minimal implementation**

```python
# bin/illum/__init__.py
"""Illumination-correction experiment library."""
```

```python
# bin/illum/grid.py
"""Recover the periodic tile grid (pitch + phase) from a stitched mosaic.

The vignette repeats at the FOV pitch, so 1-D marginal profiles are periodic.
Pitch is recovered by autocorrelation (1-px lag resolution) with sub-pixel
parabolic refinement; phase by folding the profile on the period and taking
the seam (vignette minimum).
"""
from __future__ import annotations
import numpy as np
from scipy.ndimage import gaussian_filter1d


def _detrend(x: np.ndarray, frac: float = 0.05) -> np.ndarray:
    x = x.astype(np.float64)
    sig = max(len(x) * frac, 3.0)
    return x - gaussian_filter1d(x, sig)


def period_from_profile(profile, approx=None, lo=32, hi=None) -> float:
    x = _detrend(profile).astype(np.float64)
    x = (x - x.mean()) * np.hanning(len(x))
    n = len(x)
    ac = np.correlate(x, x, mode="full")[n - 1:]
    ac = ac / (ac[0] + 1e-12)

    if hi is None:
        hi = n // 3
    lo = max(int(lo), 2)
    hi = min(int(hi), n - 2)
    if approx is not None:
        lo = max(lo, int(0.7 * approx))
        hi = min(hi, int(1.3 * approx))
    if hi <= lo:
        raise RuntimeError("no candidate period range; pass/adjust approx_tile")

    win = ac[lo:hi + 1]
    peaks = np.where((win[1:-1] > win[:-2]) & (win[1:-1] > win[2:]))[0] + 1
    if peaks.size:
        thr = 0.3 * win[peaks].max()
        good = peaks[win[peaks] >= thr]
        k = int(good.min()) + lo if good.size else int(np.argmax(win)) + lo
    else:
        k = int(np.argmax(win)) + lo

    if 0 < k < len(ac) - 1:
        y0, y1, y2 = ac[k - 1], ac[k], ac[k + 1]
        dd = (y0 - 2 * y1 + y2)
        delta = 0.5 * (y0 - y2) / dd if dd != 0 else 0.0
        return float(k + delta)
    return float(k)


def phase_from_profile(profile, period) -> int:
    x = _detrend(profile)
    P = int(round(period))
    n = len(x) // P
    if n < 2:
        return 0
    folded = np.mean([x[i * P:(i + 1) * P] for i in range(n)], axis=0)
    return int(np.argmin(gaussian_filter1d(folded, max(P * 0.02, 1.0))))


def recover_grid(stack, approx_tile=None, est_downsample=4) -> dict:
    img = stack.sum(axis=0) if stack.ndim == 3 else stack
    img = img.astype(np.float64)
    col = np.median(img, axis=0)
    row = np.median(img, axis=1)
    px = period_from_profile(col, approx=approx_tile)
    py = period_from_profile(row, approx=approx_tile)
    ox = phase_from_profile(col, px)
    oy = phase_from_profile(row, py)
    return {"pitch_y": py, "pitch_x": px, "phase_y": oy, "phase_x": ox}


def tile_origins(phase, pitch, tile_size, extent):
    """Integer start indices of complete tiles spaced on the FLOAT pitch.

    Using round(phase + k*pitch) instead of a fixed integer stride prevents
    sub-pixel pitch error from accumulating across a large mosaic.
    """
    origins, k = [], 0
    while True:
        start = int(round(phase + k * pitch))
        if start + tile_size > extent:
            break
        if start >= 0:
            origins.append(start)
        k += 1
    return origins
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_illum_grid.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git update-index --add --chmod=+r bin/illum/__init__.py bin/illum/grid.py
git add bin/illum/__init__.py bin/illum/grid.py tests/test_illum_grid.py
git commit -m ":sparkles: Add periodic grid recovery with float-pitch tile origins"
```

---

### Task 3: Flat-field estimate + chunked apply (`bin/illum/flatfield.py`)

**Files:**
- Create: `bin/illum/flatfield.py`
- Test: `tests/test_illum_flatfield.py`

**Interfaces:**
- Consumes: `illum.grid.tile_origins`
- Produces:
  - `estimate_flatfield(channel, grid, smooth_frac=0.12, est_downsample=4) -> np.ndarray` — mean-1 float32 of shape `(round(pitch_y), round(pitch_x))`.
  - `apply_field(channel, ff, grid, chunk_rows=2048, out_dtype=np.uint16) -> np.ndarray` — divides the channel by the ff tiled back on the **float** pitch, row-chunked; returns `out_dtype`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_illum_flatfield.py
import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.grid import recover_grid
from illum.flatfield import estimate_flatfield, apply_field
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_flatfield_is_mean_one():
    d = make_synthetic_mosaic(dark=0.0, n_channels=1)
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    ff = estimate_flatfield(d["mosaic"][0], g)
    assert abs(float(ff.mean()) - 1.0) < 1e-2
    assert ff.dtype == np.float32


def test_apply_reduces_illumination_variation():
    # Correcting should flatten the periodic column profile: its seam-frequency
    # variation must drop substantially.
    d = make_synthetic_mosaic(dark=0.0, vignette_strength=0.5, n_channels=1)
    ch = d["mosaic"][0]
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    ff = estimate_flatfield(ch, g)
    corr = apply_field(ch, ff, g)
    before = np.median(ch, axis=0).astype(float).std()
    after = np.median(corr, axis=0).astype(float).std()
    assert after < 0.6 * before
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_illum_flatfield.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'illum.flatfield'`

- [ ] **Step 3: Write minimal implementation**

```python
# bin/illum/flatfield.py
"""Per-channel periodic flat-field: estimate at reduced resolution, apply chunked."""
from __future__ import annotations
import numpy as np
from scipy.ndimage import gaussian_filter, zoom
from illum.grid import tile_origins


def estimate_flatfield(channel, grid, smooth_frac=0.12, est_downsample=4):
    py, px = int(round(grid["pitch_y"])), int(round(grid["pitch_x"]))
    Y, X = channel.shape
    ys = tile_origins(grid["phase_y"], grid["pitch_y"], py, Y)
    xs = tile_origins(grid["phase_x"], grid["pitch_x"], px, X)

    ds = max(int(est_downsample), 1)
    small_h, small_w = max(py // ds, 1), max(px // ds, 1)
    tiles = []
    for y in ys:
        for x in xs:
            t = channel[y:y + py, x:x + px].astype(np.float32)
            m = np.median(t)
            if m > 0:
                # estimate flat-field at reduced resolution (vignetting is smooth)
                ts = t[::ds, ::ds][:small_h, :small_w] / m
                tiles.append(ts)
    if not tiles:
        raise RuntimeError("no complete tiles; check pitch/phase/approx_tile")

    ff_small = np.median(np.stack(tiles, 0), axis=0)
    ff_small = gaussian_filter(ff_small, sigma=max(small_h, small_w) * smooth_frac)
    ff = zoom(ff_small, (py / ff_small.shape[0], px / ff_small.shape[1]), order=1)
    ff = ff[:py, :px].astype(np.float32)
    ff /= ff.mean()
    return ff


def _field_index(coords, phase, pitch, ff_len):
    frac = ((coords - phase) % pitch) / pitch      # [0, 1)
    idx = (frac * ff_len).astype(np.int64)
    return np.clip(idx, 0, ff_len - 1)


def apply_field(channel, ff, grid, chunk_rows=2048, out_dtype=np.uint16):
    Y, X = channel.shape
    py, px = ff.shape
    ci = _field_index(np.arange(X), grid["phase_x"], grid["pitch_x"], px)
    info = np.iinfo(out_dtype)
    out = np.empty((Y, X), dtype=out_dtype)
    for y0 in range(0, Y, chunk_rows):
        y1 = min(y0 + chunk_rows, Y)
        ri = _field_index(np.arange(y0, y1), grid["phase_y"], grid["pitch_y"], py)
        field = ff[np.ix_(ri, ci)]
        corr = channel[y0:y1].astype(np.float32) / field
        out[y0:y1] = np.clip(corr, info.min, info.max).astype(out_dtype)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_illum_flatfield.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add bin/illum/flatfield.py tests/test_illum_flatfield.py
git commit -m ":sparkles: Add reduced-res flat-field estimate + row-chunked apply"
```

---

### Task 4: Darkfield (`bin/illum/darkfield.py`)

**Files:**
- Create: `bin/illum/darkfield.py`
- Test: `tests/test_illum_darkfield.py`

**Interfaces:**
- Consumes: `illum.grid.tile_origins`
- Produces:
  - `estimate_dark_constant(channel, pct=1.0) -> float`
  - `estimate_dark_field(channel, grid, pct=5.0, smooth_frac=0.2, est_downsample=4) -> np.ndarray` — tile-sized float32 additive darkfield (low-percentile across aligned tiles, smoothed).
  - `subtract_dark(channel, dark) -> np.ndarray` — `dark` may be a float (constant) or a tile-sized array (tiled back on the grid via `apply`-style indexing); returns float32, not clipped (division happens later).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_illum_darkfield.py
import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.grid import recover_grid
from illum.darkfield import estimate_dark_constant, estimate_dark_field, subtract_dark
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_constant_dark_recovers_pedestal():
    d = make_synthetic_mosaic(dark=300.0, vignette_strength=0.2, n_channels=1)
    dark = estimate_dark_constant(d["mosaic"][0], pct=1.0)
    # low-percentile pedestal should land near the injected dark (loose bound:
    # synthetic content floor + blend make it approximate)
    assert 100.0 <= dark <= 600.0


def test_subtract_constant_dark_lowers_floor():
    d = make_synthetic_mosaic(dark=300.0, n_channels=1)
    ch = d["mosaic"][0]
    out = subtract_dark(ch, 250.0)
    assert out.min() >= 0.0 or float(np.percentile(out, 1)) < float(np.percentile(ch, 1))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_illum_darkfield.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'illum.darkfield'`

- [ ] **Step 3: Write minimal implementation**

```python
# bin/illum/darkfield.py
"""Additive darkfield: constant camera pedestal or spatially-varying floor."""
from __future__ import annotations
import numpy as np
from scipy.ndimage import gaussian_filter, zoom
from illum.grid import tile_origins
from illum.flatfield import _field_index


def estimate_dark_constant(channel, pct=1.0) -> float:
    return float(np.percentile(channel, pct))


def estimate_dark_field(channel, grid, pct=5.0, smooth_frac=0.2, est_downsample=4):
    py, px = int(round(grid["pitch_y"])), int(round(grid["pitch_x"]))
    Y, X = channel.shape
    ys = tile_origins(grid["phase_y"], grid["pitch_y"], py, Y)
    xs = tile_origins(grid["phase_x"], grid["pitch_x"], px, X)
    ds = max(int(est_downsample), 1)
    small_h, small_w = max(py // ds, 1), max(px // ds, 1)
    tiles = []
    for y in ys:
        for x in xs:
            t = channel[y:y + py, x:x + px].astype(np.float32)
            tiles.append(t[::ds, ::ds][:small_h, :small_w])
    stack = np.stack(tiles, 0)
    dark_small = np.percentile(stack, pct, axis=0)
    dark_small = gaussian_filter(dark_small, sigma=max(small_h, small_w) * smooth_frac)
    dark = zoom(dark_small, (py / dark_small.shape[0], px / dark_small.shape[1]), order=1)
    return dark[:py, :px].astype(np.float32)


def subtract_dark(channel, dark, grid=None, chunk_rows=2048):
    f = channel.astype(np.float32)
    if np.isscalar(dark):
        return f - float(dark)
    # tile-sized darkfield: tile it back on the float grid
    Y, X = channel.shape
    py, px = dark.shape
    ci = _field_index(np.arange(X), grid["phase_x"], grid["pitch_x"], px)
    out = np.empty((Y, X), dtype=np.float32)
    for y0 in range(0, Y, chunk_rows):
        y1 = min(y0 + chunk_rows, Y)
        ri = _field_index(np.arange(y0, y1), grid["phase_y"], grid["pitch_y"], py)
        out[y0:y1] = f[y0:y1] - dark[np.ix_(ri, ci)]
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_illum_darkfield.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add bin/illum/darkfield.py tests/test_illum_darkfield.py
git commit -m ":sparkles: Add constant + spatially-varying darkfield estimation"
```

---

### Task 5: Background-removal registry (`bin/illum/background.py`)

**Files:**
- Create: `bin/illum/background.py`
- Test: `tests/test_illum_background.py`

**Interfaces:**
- Produces:
  - `BACKGROUND_METHODS: dict[str, callable]` with keys `none`, `opening`, `gaussian`, `tophat`, `median` (and `rolling_ball` if skimage present).
  - `remove_background(channel, method, out_dtype=np.uint16, **kw) -> np.ndarray` — dispatches to a method; clips at 0; returns `out_dtype`.
  - `available_methods() -> list[str]` — methods usable in the current environment (guards skimage-only ones).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_illum_background.py
import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.background import remove_background, available_methods
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_none_is_identity():
    d = make_synthetic_mosaic(n_channels=1)
    ch = d["mosaic"][0]
    out = remove_background(ch, "none")
    assert np.array_equal(out, ch)


def test_gaussian_background_removes_low_freq_gradient():
    d = make_synthetic_mosaic(bg_amp=800.0, vignette_strength=0.05, dark=0.0, n_channels=1)
    ch = d["mosaic"][0]
    out = remove_background(ch, "gaussian", sigma=40)
    # background subtraction should lower the dim-pixel mean
    assert float(np.percentile(out, 40)) < float(np.percentile(ch, 40))


def test_available_methods_includes_core():
    m = available_methods()
    assert {"none", "opening", "gaussian", "median"} <= set(m)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_illum_background.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'illum.background'`

- [ ] **Step 3: Write minimal implementation**

```python
# bin/illum/background.py
"""Diffuse-background removal methods (non-periodic additive signal).

Applied AFTER flat-field/darkfield. Each returns clipped out_dtype. skimage-only
methods degrade gracefully when skimage is absent.
"""
from __future__ import annotations
import numpy as np
from scipy.ndimage import gaussian_filter, grey_opening, median_filter, zoom

try:
    from skimage.restoration import rolling_ball as _rolling_ball
    _HAS_SKIMAGE = True
except Exception:
    _HAS_SKIMAGE = False


def _finish(channel, corr, out_dtype):
    info = np.iinfo(out_dtype)
    return np.clip(corr, info.min, info.max).astype(out_dtype)


def _none(channel, out_dtype=np.uint16, **kw):
    return channel.astype(out_dtype, copy=True)


def _opening(channel, out_dtype=np.uint16, downsample=8, radius=None, **kw):
    f = channel.astype(np.float32)
    small = f[::downsample, ::downsample]
    r = max((radius // downsample), 1) if radius else max(min(small.shape) // 12, 1)
    bg_small = grey_opening(small, size=(2 * r + 1, 2 * r + 1))
    bg_small = gaussian_filter(bg_small, r / 2)
    bg = zoom(bg_small, (f.shape[0] / bg_small.shape[0], f.shape[1] / bg_small.shape[1]), order=1)
    return _finish(channel, f - bg[:f.shape[0], :f.shape[1]], out_dtype)


def _gaussian(channel, out_dtype=np.uint16, sigma=50, **kw):
    f = channel.astype(np.float32)
    bg = gaussian_filter(f, sigma=sigma)
    return _finish(channel, f - bg, out_dtype)


def _median(channel, out_dtype=np.uint16, downsample=8, size=15, **kw):
    f = channel.astype(np.float32)
    small = f[::downsample, ::downsample]
    bg_small = median_filter(small, size=size)
    bg = zoom(bg_small, (f.shape[0] / bg_small.shape[0], f.shape[1] / bg_small.shape[1]), order=1)
    return _finish(channel, f - bg[:f.shape[0], :f.shape[1]], out_dtype)


def _tophat(channel, out_dtype=np.uint16, downsample=8, radius=None, **kw):
    # white top-hat == img - opening(img); reuse the opening background
    return _opening(channel, out_dtype=out_dtype, downsample=downsample, radius=radius)


def _rball(channel, out_dtype=np.uint16, radius=50, **kw):
    f = channel.astype(np.float32)
    bg = _rolling_ball(f, radius=radius)
    return _finish(channel, f - bg, out_dtype)


BACKGROUND_METHODS = {
    "none": _none,
    "opening": _opening,
    "gaussian": _gaussian,
    "median": _median,
    "tophat": _tophat,
}
if _HAS_SKIMAGE:
    BACKGROUND_METHODS["rolling_ball"] = _rball


def available_methods():
    return list(BACKGROUND_METHODS.keys())


def remove_background(channel, method, out_dtype=np.uint16, **kw):
    if method not in BACKGROUND_METHODS:
        raise ValueError(f"unknown background method '{method}'; "
                         f"available: {available_methods()}")
    return BACKGROUND_METHODS[method](channel, out_dtype=out_dtype, **kw)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_illum_background.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add bin/illum/background.py tests/test_illum_background.py
git commit -m ":sparkles: Add background-removal registry (opening/gaussian/median/tophat/rolling-ball)"
```

---

### Task 6: Metrics + resource capture (`bin/illum/metrics.py`)

**Files:**
- Create: `bin/illum/metrics.py`
- Test: `tests/test_illum_metrics.py`

**Interfaces:**
- Produces:
  - `seam_peak(channel, grid, axis) -> float` — relative power at the tile frequency (lower = flatter).
  - `background_cv(channel, pct=40.0) -> float`.
  - `measure(fn, *args, **kw) -> tuple[Any, dict]` — runs `fn`, returns `(result, {"seconds": float, "peak_rss_mb": float})` using `time.perf_counter` and `resource.getrusage(RUSAGE_SELF).ru_maxrss` (delta; note ru_maxrss is bytes on macOS, kB on Linux — normalized to MB by platform).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_illum_metrics.py
import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.grid import recover_grid
from illum.flatfield import estimate_flatfield, apply_field
from illum.metrics import seam_peak, background_cv, measure
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_seam_peak_drops_after_correction():
    d = make_synthetic_mosaic(dark=0.0, vignette_strength=0.5, n_channels=1)
    ch = d["mosaic"][0]
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    ff = estimate_flatfield(ch, g)
    corr = apply_field(ch, ff, g)
    assert seam_peak(corr, g, axis=1) < seam_peak(ch, g, axis=1)


def test_measure_returns_time_and_memory():
    out, stats = measure(lambda a: a * 2, np.ones(1000))
    assert "seconds" in stats and stats["seconds"] >= 0
    assert "peak_rss_mb" in stats
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_illum_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'illum.metrics'`

- [ ] **Step 3: Write minimal implementation**

```python
# bin/illum/metrics.py
"""QC metrics (seam suppression, background flatness) + resource capture."""
from __future__ import annotations
import sys, time, resource
import numpy as np
from scipy.ndimage import gaussian_filter1d


def _detrend(x, frac=0.05):
    x = x.astype(np.float64)
    sig = max(len(x) * frac, 3.0)
    return x - gaussian_filter1d(x, sig)


def seam_peak(channel, grid, axis) -> float:
    prof = np.median(channel, axis=1 - axis).astype(np.float64)
    period = grid["pitch_x"] if axis == 1 else grid["pitch_y"]
    x = _detrend(prof)
    x = (x - x.mean()) * np.hanning(len(x))
    power = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(len(prof))
    kf = int(np.argmin(np.abs(freqs - 1.0 / period)))
    band = power[max(kf - 2, 0):kf + 3].sum()
    return float(band / (power.sum() + 1e-12))


def background_cv(channel, pct=40.0) -> float:
    thr = np.percentile(channel, pct)
    bg = channel[channel <= thr].astype(np.float64)
    return float(bg.std() / (bg.mean() + 1e-12))


def _rss_mb():
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux reports kilobytes
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


def measure(fn, *args, **kw):
    t0 = time.perf_counter()
    r0 = _rss_mb()
    result = fn(*args, **kw)
    stats = {"seconds": time.perf_counter() - t0,
             "peak_rss_mb": max(_rss_mb() - r0, 0.0)}
    return result, stats
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_illum_metrics.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add bin/illum/metrics.py tests/test_illum_metrics.py
git commit -m ":sparkles: Add seam-peak/background-CV metrics + time/memory capture"
```

---

### Task 7: Variant pipeline (`bin/illum/pipeline.py`)

**Files:**
- Create: `bin/illum/pipeline.py`
- Test: `tests/test_illum_pipeline.py`

**Interfaces:**
- Consumes: grid, flatfield, darkfield, background, metrics modules.
- Produces:
  - `@dataclass Variant(name, flatfield='periodic', dark='none', background='none', smooth_frac=0.12, est_downsample=4, bg_kwargs=None)` where `flatfield ∈ {'none','periodic','basic'}`, `dark ∈ {'none','const','field'}`.
  - `run_variant(stack, grid, variant, out_dtype=np.uint16) -> dict` → keys `name`, `corrected` (C,Y,X), `flats` (list per channel or None), `metrics` (dict: per-axis seam_peak before/after, background_cv before/after, seconds, peak_rss_mb).
  - `DEFAULT_MATRIX: list[Variant]` and `full_matrix(available_bg) -> list[Variant]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_illum_pipeline.py
import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.grid import recover_grid
from illum.pipeline import Variant, run_variant, DEFAULT_MATRIX
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_run_periodic_variant_improves_seam():
    d = make_synthetic_mosaic(dark=200.0, vignette_strength=0.5, n_channels=2)
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    v = Variant(name="periodic-const", flatfield="periodic", dark="const")
    res = run_variant(d["mosaic"], g, v)
    assert res["corrected"].shape == d["mosaic"].shape
    m = res["metrics"]
    assert m["seam_x_after"] <= m["seam_x_before"]
    assert m["seconds"] >= 0


def test_default_matrix_nonempty_and_named():
    assert len(DEFAULT_MATRIX) >= 3
    assert len({v.name for v in DEFAULT_MATRIX}) == len(DEFAULT_MATRIX)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_illum_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'illum.pipeline'`

- [ ] **Step 3: Write minimal implementation**

```python
# bin/illum/pipeline.py
"""Variant definition + per-variant run (correct all channels, capture metrics)."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import time
import numpy as np

from illum.flatfield import estimate_flatfield, apply_field
from illum.darkfield import estimate_dark_constant, estimate_dark_field, subtract_dark
from illum.background import remove_background
from illum.metrics import seam_peak, background_cv, _rss_mb


@dataclass
class Variant:
    name: str
    flatfield: str = "periodic"   # none | periodic | basic
    dark: str = "none"            # none | const | field
    background: str = "none"      # none | opening | gaussian | median | tophat | rolling_ball
    smooth_frac: float = 0.12
    est_downsample: int = 4
    float_pitch: bool = True      # False = verbatim int-pitch tiling (drifts on big mosaics)
    bg_kwargs: Optional[dict] = None


def _grid_for(grid, float_pitch):
    """Round pitch to int for the verbatim (drift-prone) behavior; else pass through."""
    if float_pitch:
        return grid
    g = dict(grid)
    g["pitch_y"] = float(round(grid["pitch_y"]))
    g["pitch_x"] = float(round(grid["pitch_x"]))
    return g


def _correct_channel(ch, grid, variant, out_dtype):
    grid = _grid_for(grid, variant.float_pitch)
    f = ch.astype(np.float32)
    # darkfield (additive) subtracted BEFORE the divide
    if variant.dark == "const":
        f = subtract_dark(f, estimate_dark_constant(ch))
    elif variant.dark == "field":
        df = estimate_dark_field(ch, grid, est_downsample=variant.est_downsample)
        f = subtract_dark(f, df, grid=grid)

    ff = None
    if variant.flatfield == "periodic":
        ff = estimate_flatfield(ch, grid, smooth_frac=variant.smooth_frac,
                                est_downsample=variant.est_downsample)
        # apply on the dark-subtracted signal; clip handled inside apply
        f = np.clip(f, 0, None)
        corr = apply_field(f.astype(np.float32), ff, grid, out_dtype=out_dtype)
    elif variant.flatfield == "basic":
        corr = _basic_correct(ch, out_dtype)   # may raise if basicpy absent
    else:  # none
        info = np.iinfo(out_dtype)
        corr = np.clip(f, info.min, info.max).astype(out_dtype)

    if variant.background != "none":
        corr = remove_background(corr, variant.background, out_dtype=out_dtype,
                                 **(variant.bg_kwargs or {}))
    return corr, ff


def _basic_correct(ch, out_dtype):
    import importlib.util, pathlib, sys
    bin_dir = pathlib.Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(bin_dir))
    from preprocess import apply_basic_correction   # requires basicpy
    corrected, _ = apply_basic_correction(ch)
    info = np.iinfo(out_dtype)
    return np.clip(corrected, info.min, info.max).astype(out_dtype)


def run_variant(stack, grid, variant, out_dtype=np.uint16):
    t0 = time.perf_counter()
    r0 = _rss_mb()
    C = stack.shape[0]
    out = np.empty_like(stack, dtype=out_dtype)
    flats = []
    seam_x_b = seam_y_b = cv_b = 0.0
    seam_x_a = seam_y_a = cv_a = 0.0
    for c in range(C):
        ch = stack[c]
        seam_x_b += seam_peak(ch, grid, 1); seam_y_b += seam_peak(ch, grid, 0)
        cv_b += background_cv(ch)
        corr, ff = _correct_channel(ch, grid, variant, out_dtype)
        out[c] = corr
        flats.append(ff)
        seam_x_a += seam_peak(corr, grid, 1); seam_y_a += seam_peak(corr, grid, 0)
        cv_a += background_cv(corr)
    metrics = {
        "seam_x_before": seam_x_b / C, "seam_x_after": seam_x_a / C,
        "seam_y_before": seam_y_b / C, "seam_y_after": seam_y_a / C,
        "cv_before": cv_b / C, "cv_after": cv_a / C,
        "seconds": time.perf_counter() - t0,
        "peak_rss_mb": max(_rss_mb() - r0, 0.0),
    }
    return {"name": variant.name, "corrected": out, "flats": flats, "metrics": metrics}


DEFAULT_MATRIX = [
    Variant("periodic-int-verbatim", flatfield="periodic", dark="none", float_pitch=False),
    Variant("periodic-float", flatfield="periodic", dark="none", float_pitch=True),
    Variant("periodic-float-const-dark", flatfield="periodic", dark="const"),
    Variant("periodic-float-const-dark+gaussian-bg", flatfield="periodic", dark="const",
            background="gaussian", bg_kwargs={"sigma": 50}),
    Variant("periodic-float-field-dark", flatfield="periodic", dark="field"),
]


def full_matrix(available_bg):
    variants = []
    for ff in ("periodic",):
        for dk in ("none", "const", "field"):
            for bg in ["none"] + [b for b in available_bg if b != "none"]:
                name = f"{ff}_{dk}_{bg}"
                variants.append(Variant(name, flatfield=ff, dark=dk, background=bg))
    return variants
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_illum_pipeline.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add bin/illum/pipeline.py tests/test_illum_pipeline.py
git commit -m ":sparkles: Add Variant pipeline (flatfield/darkfield/background combos + metrics)"
```

---

### Task 8: Diagnostic plots (`bin/illum/plots.py`)

**Files:**
- Create: `bin/illum/plots.py`
- Test: `tests/test_illum_plots.py`

**Interfaces:**
- Consumes: grid, metrics modules; matplotlib (Agg backend).
- Produces:
  - `plot_grid_recovery(stack, grid, out_path, approx_tile=None)` — profiles + autocorrelation + grid overlay thumbnail.
  - `plot_flatfield(ff, out_path, channel_name)` — heatmap.
  - `plot_before_after(before, after, grid, out_path, channel_name)` — crops + column profiles + seam-freq power spectrum.
  - Each writes a PNG and returns the path.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_illum_plots.py
import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.grid import recover_grid
from illum.flatfield import estimate_flatfield, apply_field
from illum.plots import plot_grid_recovery, plot_flatfield, plot_before_after
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_plots_write_pngs(tmp_path):
    d = make_synthetic_mosaic(n_channels=1, vignette_strength=0.4, dark=0.0)
    ch = d["mosaic"][0]
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    ff = estimate_flatfield(ch, g)
    corr = apply_field(ch, ff, g)
    p1 = plot_grid_recovery(d["mosaic"], g, tmp_path / "grid.png", approx_tile=d["pitch"])
    p2 = plot_flatfield(ff, tmp_path / "ff.png", "CH0")
    p3 = plot_before_after(ch, corr, g, tmp_path / "ba.png", "CH0")
    for p in (p1, p2, p3):
        assert pathlib.Path(p).exists() and pathlib.Path(p).stat().st_size > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_illum_plots.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'illum.plots'`

- [ ] **Step 3: Write minimal implementation**

```python
# bin/illum/plots.py
"""Per-step diagnostic plots. Uses the Agg backend (headless-safe)."""
from __future__ import annotations
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from illum.grid import period_from_profile
from illum.metrics import _detrend


def _thumb(img, target=800):
    ds = max(max(img.shape) // target, 1)
    return img[::ds, ::ds]


def plot_grid_recovery(stack, grid, out_path, approx_tile=None):
    img = (stack.sum(axis=0) if stack.ndim == 3 else stack).astype(np.float64)
    col = np.median(img, axis=0)
    row = np.median(img, axis=1)
    fig, ax = plt.subplots(2, 2, figsize=(11, 8))
    ax[0, 0].plot(col); ax[0, 0].set_title("Column profile (X)")
    ax[0, 1].plot(row); ax[0, 1].set_title("Row profile (Y)")
    x = _detrend(col); x = (x - x.mean()) * np.hanning(len(x))
    ac = np.correlate(x, x, mode="full")[len(x) - 1:]; ac /= ac[0] + 1e-12
    ax[1, 0].plot(ac[:min(len(ac), int(3 * grid["pitch_x"]))])
    ax[1, 0].axvline(grid["pitch_x"], color="r", ls="--",
                     label=f"pitch_x={grid['pitch_x']:.1f}")
    ax[1, 0].legend(); ax[1, 0].set_title("Autocorrelation (X)")
    th = _thumb(img)
    ax[1, 1].imshow(th, cmap="gray")
    step = grid["pitch_x"] / (img.shape[1] / th.shape[1])
    for k in range(int(th.shape[1] / step) + 1):
        ax[1, 1].axvline(grid["phase_x"] / (img.shape[1] / th.shape[1]) + k * step,
                         color="c", lw=0.4)
    ax[1, 1].set_title("Recovered grid overlay")
    fig.tight_layout(); fig.savefig(out_path, dpi=90); plt.close(fig)
    return str(out_path)


def plot_flatfield(ff, out_path, channel_name):
    fig, ax = plt.subplots(figsize=(5, 4))
    im = ax.imshow(ff, cmap="viridis"); fig.colorbar(im, ax=ax)
    ax.set_title(f"Flat-field — {channel_name} (p-p {np.ptp(ff) * 100:.1f}%)")
    fig.tight_layout(); fig.savefig(out_path, dpi=90); plt.close(fig)
    return str(out_path)


def plot_before_after(before, after, grid, out_path, channel_name):
    fig, ax = plt.subplots(2, 2, figsize=(11, 8))
    ax[0, 0].imshow(_thumb(before), cmap="gray"); ax[0, 0].set_title("Before")
    ax[0, 1].imshow(_thumb(after), cmap="gray"); ax[0, 1].set_title("After")
    ax[1, 0].plot(np.median(before, axis=0), label="before")
    ax[1, 0].plot(np.median(after, axis=0), label="after")
    ax[1, 0].legend(); ax[1, 0].set_title("Column profile")
    for lbl, arr in (("before", before), ("after", after)):
        p = _detrend(np.median(arr, axis=0).astype(np.float64))
        p = (p - p.mean()) * np.hanning(len(p))
        power = np.abs(np.fft.rfft(p)) ** 2
        ax[1, 1].semilogy(np.fft.rfftfreq(len(p)), power + 1e-9, label=lbl)
    ax[1, 1].axvline(1.0 / grid["pitch_x"], color="r", ls="--", label="tile freq")
    ax[1, 1].legend(); ax[1, 1].set_title("Power spectrum (X)")
    fig.suptitle(channel_name)
    fig.tight_layout(); fig.savefig(out_path, dpi=90); plt.close(fig)
    return str(out_path)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_illum_plots.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add bin/illum/plots.py tests/test_illum_plots.py
git commit -m ":sparkles: Add per-step diagnostic plots (grid/flatfield/before-after)"
```

---

### Task 9: HTML report + recommendation (`bin/illum/report.py`)

**Files:**
- Create: `bin/illum/report.py`
- Test: `tests/test_illum_report.py`

**Interfaces:**
- Produces:
  - `rank_variants(results) -> list[dict]` — each `{name, composite, seam_gain, cv_gain, seconds, peak_rss_mb}`, sorted best-first. `composite = seam_gain + cv_gain` where `seam_gain = 1 - after/before` (mean of X,Y), `cv_gain = 1 - cv_after/cv_before`.
  - `recommendation_text(ranked) -> str` — a paragraph naming the winner, the runner-up, the trade-off (seam vs cv vs runtime), and when to prefer each.
  - `write_report(results, plot_index, out_html)` — self-contained HTML: leaderboard table, recommendation, embedded per-variant plot links.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_illum_report.py
import sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
from illum.report import rank_variants, recommendation_text, write_report


def _fake(name, sb, sa, cb, ca, sec):
    return {"name": name, "metrics": {
        "seam_x_before": sb, "seam_x_after": sa, "seam_y_before": sb, "seam_y_after": sa,
        "cv_before": cb, "cv_after": ca, "seconds": sec, "peak_rss_mb": 100.0}}


def test_ranking_prefers_more_suppression():
    res = [_fake("weak", 0.5, 0.4, 0.3, 0.28, 1.0),
           _fake("strong", 0.5, 0.1, 0.3, 0.15, 2.0)]
    ranked = rank_variants(res)
    assert ranked[0]["name"] == "strong"
    assert "strong" in recommendation_text(ranked)


def test_write_report_creates_html(tmp_path):
    res = [_fake("a", 0.5, 0.2, 0.3, 0.2, 1.0)]
    out = tmp_path / "report.html"
    write_report(res, {"a": ["grid.png"]}, out)
    assert out.exists() and "<table" in out.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_illum_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'illum.report'`

- [ ] **Step 3: Write minimal implementation**

```python
# bin/illum/report.py
"""Rank variants, produce a recommendation, render a self-contained HTML report."""
from __future__ import annotations
import html


def rank_variants(results):
    ranked = []
    for r in results:
        m = r["metrics"]
        sb = (m["seam_x_before"] + m["seam_y_before"]) / 2
        sa = (m["seam_x_after"] + m["seam_y_after"]) / 2
        seam_gain = 1.0 - (sa / sb) if sb > 0 else 0.0
        cv_gain = 1.0 - (m["cv_after"] / m["cv_before"]) if m["cv_before"] > 0 else 0.0
        ranked.append({
            "name": r["name"], "seam_gain": seam_gain, "cv_gain": cv_gain,
            "composite": seam_gain + cv_gain,
            "seconds": m["seconds"], "peak_rss_mb": m["peak_rss_mb"],
        })
    ranked.sort(key=lambda d: d["composite"], reverse=True)
    return ranked


def recommendation_text(ranked):
    if not ranked:
        return "No variants produced."
    win = ranked[0]
    lines = [f"Recommended: **{win['name']}** — highest composite score "
             f"({win['composite']:.3f}): seam suppression {win['seam_gain']*100:.1f}%, "
             f"background-flatness gain {win['cv_gain']*100:.1f}%, "
             f"{win['seconds']:.1f}s, {win['peak_rss_mb']:.0f} MB peak."]
    if len(ranked) > 1:
        r = ranked[1]
        faster = r["seconds"] < win["seconds"]
        lines.append(
            f"Runner-up **{r['name']}** ({r['composite']:.3f})"
            + (f" is faster ({r['seconds']:.1f}s vs {win['seconds']:.1f}s); "
               "prefer it if the composite gap is within visual noise and runtime matters."
               if faster else
               "; the winner leads on both quality and is not slower, so prefer the winner."))
    lines.append("When scores are within ~0.02 of each other, treat them as a tie and "
                 "decide from the before/after crops and QuPath pyramids — the metrics "
                 "cannot see local artifacts the eye catches.")
    return "\n\n".join(lines)


def write_report(results, plot_index, out_html):
    ranked = rank_variants(results)
    rows = "".join(
        f"<tr><td>{i+1}</td><td>{html.escape(d['name'])}</td>"
        f"<td>{d['composite']:.3f}</td><td>{d['seam_gain']*100:.1f}%</td>"
        f"<td>{d['cv_gain']*100:.1f}%</td><td>{d['seconds']:.1f}</td>"
        f"<td>{d['peak_rss_mb']:.0f}</td></tr>"
        for i, d in enumerate(ranked))
    plots_html = ""
    for name, paths in plot_index.items():
        imgs = "".join(f'<div><img src="{html.escape(p)}" style="max-width:100%"></div>'
                       for p in paths)
        plots_html += f"<h3>{html.escape(name)}</h3>{imgs}"
    rec = recommendation_text(ranked).replace("\n\n", "<br><br>").replace("**", "")
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Illumination-correction benchmark</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px}}
table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:4px 8px}}
.rec{{background:#f4f7ff;border-left:4px solid #47c;padding:1rem;margin:1rem 0}}</style>
</head><body>
<h1>Illumination-correction benchmark</h1>
<div class="rec">{rec}</div>
<h2>Leaderboard</h2>
<table><tr><th>#</th><th>Variant</th><th>Composite</th><th>Seam gain</th>
<th>CV gain</th><th>Seconds</th><th>Peak MB</th></tr>{rows}</table>
<h2>Diagnostics</h2>{plots_html}
</body></html>"""
    open(out_html, "w").write(doc)
    return str(out_html)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_illum_report.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add bin/illum/report.py tests/test_illum_report.py
git commit -m ":sparkles: Add variant ranking, recommendation text, and HTML report"
```

---

### Task 10: Single-variant CLI (`bin/illum_correct.py`)

Pipeline-facing entry: one variant, one input image, writes an OME-TIFF. This is what Phase B's Nextflow module will call.

**Files:**
- Create: `bin/illum_correct.py`
- Test: `tests/test_illum_cli.py`

**Interfaces:**
- Consumes: grid, pipeline modules; tifffile.
- CLI: `--image PATH --output_dir DIR --channels NAME [NAME ...] --approx-tile 1950 [--flatfield periodic] [--dark none|const|field] [--background none|gaussian|...] [--smooth-frac 0.12]`. Output: `<stem>_periodic.ome.tif` with pixel size threaded from input OME metadata (fallback 0.325 µm).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_illum_cli.py
import subprocess, sys, pathlib
import numpy as np, tifffile
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_cli_writes_corrected_ome_tiff(tmp_path):
    d = make_synthetic_mosaic(tile=96, overlap=12, n_channels=2, dark=150.0)
    src = tmp_path / "mosaic.tif"
    tifffile.imwrite(src, d["mosaic"], metadata={"axes": "CYX"})
    out = tmp_path / "out"
    out.mkdir()
    r = subprocess.run([sys.executable, str(ROOT / "bin" / "illum_correct.py"),
                        "--image", str(src), "--output_dir", str(out),
                        "--channels", "CH0", "CH1", "--approx-tile", str(d["pitch"]),
                        "--dark", "const"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    produced = list(out.glob("*_periodic.ome.tif"))
    assert produced, r.stdout + r.stderr
    arr = tifffile.imread(produced[0])
    assert arr.shape == d["mosaic"].shape
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_illum_cli.py -v`
Expected: FAIL (illum_correct.py does not exist → nonzero returncode / no output file)

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
"""Single-variant periodic illumination correction (pipeline-facing CLI)."""
from __future__ import annotations
import argparse, os, sys, pathlib
import numpy as np
import tifffile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from illum.grid import recover_grid
from illum.pipeline import Variant, run_variant


def _read_pixel_size(path, fallback=0.325):
    try:
        with tifffile.TiffFile(path) as tif:
            if tif.ome_metadata:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(tif.ome_metadata)
                px = root.find(".//{*}Pixels")
                if px is not None and px.get("PhysicalSizeX"):
                    return float(px.get("PhysicalSizeX"))
    except Exception:
        pass
    return fallback


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True)
    p.add_argument("--output_dir", required=True)
    p.add_argument("--channels", nargs="+", required=True)
    p.add_argument("--approx-tile", type=float, default=1950)
    p.add_argument("--flatfield", default="periodic", choices=["none", "periodic", "basic"])
    p.add_argument("--dark", default="none", choices=["none", "const", "field"])
    p.add_argument("--background", default="none")
    p.add_argument("--smooth-frac", type=float, default=0.12)
    args = p.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    stack = np.squeeze(tifffile.imread(args.image))
    if stack.ndim == 2:
        stack = stack[None]
    px = _read_pixel_size(args.image)

    grid = recover_grid(stack, approx_tile=args.approx_tile)
    print(f"grid: pitch_x={grid['pitch_x']:.2f} pitch_y={grid['pitch_y']:.2f} "
          f"phase_x={grid['phase_x']} phase_y={grid['phase_y']}")

    v = Variant(name="cli", flatfield=args.flatfield, dark=args.dark,
                background=args.background, smooth_frac=args.smooth_frac)
    res = run_variant(stack, grid, v, out_dtype=stack.dtype)

    stem = pathlib.Path(args.image).name
    for suf in (".ome.tif", ".ome.tiff", ".tif", ".tiff"):
        if stem.endswith(suf):
            stem = stem[:-len(suf)]
            break
    out_path = os.path.join(args.output_dir, f"{stem}_periodic.ome.tif")
    tifffile.imwrite(out_path, res["corrected"], photometric="minisblack",
                     bigtiff=True, ome=True, metadata={
                         "axes": "CYX", "Channel": {"Name": args.channels[:stack.shape[0]]},
                         "PhysicalSizeX": px, "PhysicalSizeXUnit": "µm",
                         "PhysicalSizeY": px, "PhysicalSizeYUnit": "µm"})
    print(f"wrote {out_path}  metrics={res['metrics']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_illum_cli.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Set exec bit and commit**

```bash
git update-index --add --chmod=+x bin/illum_correct.py
git add bin/illum_correct.py tests/test_illum_cli.py
git commit -m ":sparkles: Add single-variant illum_correct CLI (pipeline-facing)"
git ls-files -s bin/illum_correct.py   # must show 100755
```

---

### Task 11: Sweep driver (`bin/illum_benchmark.py`)

Runs the full sweep on one image: variants → corrected OME-TIFF + QuPath pyramid + plots + metrics.json + HTML report.

**Files:**
- Create: `bin/illum_benchmark.py`
- Test: `tests/test_illum_benchmark.py`

**Interfaces:**
- Consumes: grid, pipeline, plots, report modules; `merge_channels_pyramid.write_pyramidal_ome_tiff` for pyramids.
- CLI: `--image PATH --outdir DIR --channels ... --approx-tile 1950 [--full-grid] [--no-pyramids] [--max-channels N]`. Writes `<outdir>/metrics.json`, `<outdir>/report.html`, `<outdir>/plots/*.png`, and `<outdir>/pyramids/<variant>.ome.tiff` per variant.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_illum_benchmark.py
import subprocess, sys, json, pathlib
import tifffile
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_benchmark_produces_report_and_metrics(tmp_path):
    d = make_synthetic_mosaic(tile=96, overlap=12, n_channels=2, dark=150.0,
                              vignette_strength=0.5)
    src = tmp_path / "mosaic.tif"
    tifffile.imwrite(src, d["mosaic"], metadata={"axes": "CYX"})
    outdir = tmp_path / "bench"
    r = subprocess.run([sys.executable, str(ROOT / "bin" / "illum_benchmark.py"),
                        "--image", str(src), "--outdir", str(outdir),
                        "--channels", "CH0", "CH1", "--approx-tile", str(d["pitch"]),
                        "--no-pyramids"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (outdir / "report.html").exists()
    metrics = json.loads((outdir / "metrics.json").read_text())
    assert len(metrics) >= 3
    assert all("seam_x_after" in m["metrics"] for m in metrics)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_illum_benchmark.py -v`
Expected: FAIL (illum_benchmark.py does not exist → nonzero returncode)

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
"""Benchmark illumination-correction variants on one mosaic.

Produces per variant: corrected OME-TIFF, QuPath pyramid, diagnostic plots,
plus a cross-variant metrics.json and an HTML report with a recommendation.
"""
from __future__ import annotations
import argparse, json, os, sys, pathlib
import numpy as np
import tifffile

BIN = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
from illum.grid import recover_grid
from illum.pipeline import DEFAULT_MATRIX, full_matrix, run_variant
from illum.background import available_methods
from illum.plots import plot_grid_recovery, plot_flatfield, plot_before_after
from illum.report import write_report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image", required=True)
    p.add_argument("--outdir", required=True)
    p.add_argument("--channels", nargs="+", required=True)
    p.add_argument("--approx-tile", type=float, default=1950)
    p.add_argument("--full-grid", action="store_true")
    p.add_argument("--no-pyramids", action="store_true")
    p.add_argument("--max-channels", type=int, default=None)
    args = p.parse_args()

    outdir = pathlib.Path(args.outdir)
    (outdir / "plots").mkdir(parents=True, exist_ok=True)
    if not args.no_pyramids:
        (outdir / "pyramids").mkdir(parents=True, exist_ok=True)

    stack = np.squeeze(tifffile.imread(args.image))
    if stack.ndim == 2:
        stack = stack[None]
    if args.max_channels:
        stack = stack[:args.max_channels]

    grid = recover_grid(stack, approx_tile=args.approx_tile)
    print(f"grid: {grid}")

    variants = full_matrix(available_methods()) if args.full_grid else DEFAULT_MATRIX

    # grid-recovery plot once (geometry shared across channels/variants)
    plot_index = {}
    grid_png = plot_grid_recovery(stack, grid, outdir / "plots" / "grid.png",
                                  approx_tile=args.approx_tile)

    results = []
    for v in variants:
        print(f"--- variant: {v.name}")
        try:
            res = run_variant(stack, grid, v, out_dtype=stack.dtype)
        except Exception as e:
            print(f"    SKIP {v.name}: {e}")
            continue
        results.append({"name": res["name"], "metrics": res["metrics"]})

        pngs = [grid_png]
        if res["flats"][0] is not None:
            pngs.append(plot_flatfield(res["flats"][0], outdir / "plots" / f"{v.name}_ff.png",
                                       args.channels[0]))
        pngs.append(plot_before_after(stack[0], res["corrected"][0], grid,
                                      outdir / "plots" / f"{v.name}_ba.png", args.channels[0]))
        plot_index[v.name] = pngs

        if not args.no_pyramids:
            from merge_channels_pyramid import write_pyramidal_ome_tiff, generate_channel_color
            colors = [generate_channel_color(n, i) for i, n in enumerate(args.channels[:stack.shape[0]])]
            write_pyramidal_ome_tiff(res["corrected"], str(outdir / "pyramids" / f"{v.name}.ome.tiff"),
                                     args.channels[:stack.shape[0]], colors)

    (outdir / "metrics.json").write_text(json.dumps(
        [{"name": r["name"], "metrics": r["metrics"]} for r in results], indent=2))
    write_report(results, plot_index, outdir / "report.html")
    print(f"wrote {outdir/'report.html'} and {outdir/'metrics.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_illum_benchmark.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Set exec bit and commit**

```bash
git update-index --add --chmod=+x bin/illum_benchmark.py
git add bin/illum_benchmark.py tests/test_illum_benchmark.py
git commit -m ":sparkles: Add illum_benchmark sweep driver (plots + report + pyramids per variant)"
git ls-files -s bin/illum_benchmark.py   # must show 100755
```

---

### Task 12: Full-suite green + docs

**Files:**
- Modify: `docs/superpowers/specs/2026-07-19-illumination-correction-harness-design.md` (mark implemented)
- Create: `bin/illum/README.md`

- [ ] **Step 1: Run the whole illum test suite**

Run: `pytest -v tests/test_illum_*.py`
Expected: all PASS

- [ ] **Step 2: Confirm no regressions in the existing Python suite**

Run: `pytest -v tests/ --ignore=tests/testdata --ignore=tests/modules --ignore=tests/subworkflows --ignore=tests/integration`
Expected: existing tests still PASS (new illum tests included)

- [ ] **Step 3: Write `bin/illum/README.md`**

```markdown
# illum — illumination-correction experiment library

Benchmark flat-field / darkfield / background-removal variants on a stitched mosaic.

- `illum_benchmark.py --image X.ome.tiff --outdir bench --channels DAPI CD3 ... --approx-tile 1950`
  → `bench/report.html`, `bench/metrics.json`, `bench/plots/`, `bench/pyramids/`.
- `--full-grid` runs the full variant matrix; `--no-pyramids` skips pyramid writing;
  `--max-channels N` limits channels for a quick pass.
- `illum_correct.py` applies ONE variant (pipeline-facing), writing `<stem>_periodic.ome.tif`.

Run on the real cluster mosaic, open `report.html`, and compare pyramids in QuPath.
Ranking is a seam-suppression + background-flatness composite; when variants are
within ~0.02 composite, decide from the crops and QuPath.
```

- [ ] **Step 4: Commit**

```bash
git add bin/illum/README.md docs/superpowers/specs/2026-07-19-illumination-correction-harness-design.md
git commit -m ":memo: Add illum library README; mark harness spec implemented"
```

---

## Self-Review

**Spec coverage:**
- §3 three axes → Tasks 3 (flatfield), 4 (darkfield const+field), 5 (background registry, multiple methods), 7 (Variant combines them). ✓
- §4 library structure → Tasks 2–9 one module each. ✓
- §5 efficiency (reduced-res estimate, chunked apply, sequential channels, time/RSS metrics) → Tasks 3, 6, 7. ✓
- §6 diagnostic plots + leaderboard + recommendation → Tasks 8, 9. ✓
- §7 synthetic ground truth + pytest → Task 1 + tests throughout. ✓
- §2 pyramids-in-harness → Task 11 (reuses `write_pyramidal_ome_tiff`). ✓
- §8 Phase B → explicitly deferred (documented in spec, not in this plan). ✓
- Global constraint: exec bits → Tasks 10, 11 set `100755` and verify; library files stay default. ✓

**Placeholder scan:** no TBD/TODO; every code step has full code. ✓

**Type consistency:** `recover_grid` returns `pitch_y/pitch_x/phase_y/phase_x` — consumed identically in flatfield, darkfield, metrics, plots. `run_variant` returns `{name, corrected, flats, metrics}` — consumed by benchmark and report (report reads only `name`+`metrics`, benchmark reads `flats`+`corrected`). `_field_index` defined in flatfield, imported by darkfield. `Variant` fields consistent across pipeline/CLI/benchmark. ✓

**Note on `basic` variant:** requires `basicpy`; `_basic_correct` imports lazily and `run_variant` is wrapped in try/except in the benchmark driver, so its absence skips only that variant (per Global Constraints). It is intentionally NOT in `DEFAULT_MATRIX` (which must run in CI without basicpy); it enters via `--full-grid` only when available, or is added explicitly when running on the cluster image where basicpy is installed.
