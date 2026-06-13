# Nextflow-Parallel VALIS Tiled Non-Rigid Registration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in registration path that lifts VALIS 1.0.0's in-process tiled non-rigid
tile loop into Nextflow processes (one task per tile), producing **bit-identical** output to
classic VALIS in the tiling regime, gated by `params.reg_distributed_tiling`.

**Architecture:** Strategy 2 from the spec, decomposed per-step for low-budget clusters (spec §5C).
`REG_PREP` runs VALIS through **rigid registration only** (JVM-backed), materializes the per-slide
non-rigid tiler inputs (rigid-warped moving image downsampled to `max_non_rigid_registration_dim_px`,
fixed image, mask, `target_stats`, processing config) and the `expanded_bboxes` tile grid, then
**halts before computing any tiles** (dump-mode `calc()` raises after dumping) and pickles the
registrar. `REG_TILE` computes one tile's displacement field via VALIS's **own**
`NonRigidTileRegistrar.reg_tile` — **no JVM, no BioFormats, ~1–2 GB** (the cheap-node fan-out unit,
spec §5C). `REG_FINALIZE` reloads VALIS state, sets the **explicit `EXTERNAL_TILE_HOOK`** (spec §6 Option B —
a ~4-line patched seam, not runtime monkeypatch) so `calc()` **reads** the precomputed tiles, runs
VALIS's own `stitch_tiles` + displacement composition, then (if micro is on, spec §5A)
`register_micro()` in-process, then `warp_and_save_slide`. All VALIS math stays inside VALIS.

**Why these stages and not more (spec §5B):** feature detection / matching / rigid run on the
*downsampled* `processed_img` (~1–2k px) — fixed, tiny RAM regardless of slide size — so they are
**not** tiled (tiling them buys ~0 memory and would seam the global alignment). Only non-rigid +
micro scale with image size, so only they are externalized.

**Tech Stack:** Nextflow DSL2 (`>=25.04.0`, nf-boost), Python 3.10, `valis-wsi==1.0.0`, pyvips,
tifffile, numpy; nf-test (stub + real), pytest. Reference source: `valis_lib/` (== pip 1.0.0).

**Spec:** `docs/superpowers/specs/2026-06-13-valis-tiled-parallel-registration-design.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `bin/utils/tile_grid.py` | Pure: compute `expanded_bboxes` grid + manifest JSON (wraps VALIS `get_grid_bboxes`/`expand_bbox`) | Create |
| `bin/utils/tile_io.py` | Pure: lossless float32 per-tile displacement-field write/read (vips) | Create |
| `containers/valis/calc_hook.patch` | ~4-line reviewable diff adding `NonRigidTileRegistrar.EXTERNAL_TILE_HOOK` (default `None`) to pip VALIS; applied in the Dockerfile after `pip install` (spec §6 Option B) | Create |
| `bin/utils/valis_tiling.py` | Sets/clears `EXTERNAL_TILE_HOOK`: **halt hook** (dump inputs + raise, used by PREP) + **read hook** (inject tiles, used by FINALIZE). The only module that knows the seam. | Create |
| `bin/reg_prep.py` | Stage 1 (JVM): VALIS **rigid only** → dump tiler inputs + manifest + pickle registrar, halt before tile compute | Create |
| `bin/reg_tile.py` | Stage 2 (**no JVM**, ~1–2 GB): compute one tile via VALIS `reg_tile` | Create |
| `bin/reg_finalize.py` | Stage 3 (JVM): reload VALIS, inject tiles, stitch+compose, micro (if on), warp | Create |
| `modules/local/reg_prep.nf` | `REG_PREP` process | Create |
| `modules/local/reg_tile.nf` | `REG_TILE` process (fan-out) | Create |
| `modules/local/reg_finalize.nf` | `REG_FINALIZE` process (fan-in) | Create |
| `subworkflows/local/registration.nf` | Branch classic vs distributed on `params.reg_distributed_tiling` | Modify |
| `nextflow.config` | Param defaults | Modify |
| `nextflow_schema.json` | Param schema | Modify |
| `containers/valis/Dockerfile` | Pin `valis-wsi==1.0.0`; apply `calc_hook.patch` over the installed VALIS | Modify |
| `docs/registration_methods.md` | Document the distributed path | Modify |
| `tests/unit/test_tile_grid.py` | Grid math equality vs VALIS | Create |
| `tests/unit/test_tile_io.py` | Float32 round-trip equality | Create |
| `tests/modules/reg_prep.nf.test` etc. | nf-test stubs | Create |
| `tests/integration/test_bit_identical.py` | Golden classic-vs-distributed pixel diff | Create |

---

## Phase 0 — De-risk the integration crux

> ### ✅ Task 1 RESULT (2026-06-14) — see spec §6.1 for the full decision record
> - **Make-or-break PROVEN:** externalized `reg_tile` (separate OS processes) + VALIS
>   `stitch_tiles` == in-process `calc()`, **bit-identical** (`max|Δ|=0` on a real 3×3 grid, bk+fwd).
>   Strategy 2's tile externalization is sound → **Phase 1 unblocked.**
> - **Three blockers found, forcing Phase-2 revisions (NOT Phase-1 blockers):**
>   1. **pyvips images are unpicklable** ⇒ the pickled-registrar PREP→FINALIZE handoff (§5C) is
>      dead. Exchange **plain data** (`.npy`/JSON/`.v`), never a pickled `Valis`. FINALIZE
>      reconstructs composition+warp from dumped rigid state. **Revises Tasks 4, 5, 7.**
>   2. **`register()` swallows all exceptions** ⇒ `install_halt_hook` cannot raise-to-halt; signal
>      halt by filesystem side-effect. **Revises Task 4 (`valis_tiling.py`) + Task 5.**
>   3. **`ChannelGetter` crashes in `process_tile` (`src_f=None`)** ⇒ classic tiling is broken for
>      fluorescence; feed the tiler pre-reduced 2-D channels with `processing_cls=None`, and verify
>      fluorescence against **whole-image** classic. **Revises Tasks 5, 6, 10.**
> - Spike kept at `bin/spikes/spike_externalize_tiles.py` as the reproduction + regression harness.

### Task 1: Spike — externalize-and-resume proof of concept

**Goal:** Before any Nextflow wiring, prove on real test data that we can (a) intercept
`NonRigidTileRegistrar.calc()` to dump its inputs and halt, (b) recompute one tile in a separate
process via `reg_tile`, and (c) re-enter VALIS with `calc()` reading the precomputed tiles, and
that the resulting stitched `bk_dxdy` equals an unmodified VALIS run **exactly**. This validates
Strategy 2 (and surfaces early if registrar pickle-resume is infeasible → fall back to the
documented rigid/non-rigid split).

**Files:**
- Create: `bin/spikes/spike_externalize_tiles.py` (throwaway; deleted at end of task)
- Reference: `valis_lib/non_rigid_registrars.py`, `valis_lib/registration.py`

> **Seam note:** the spike bootstraps with a **runtime** `calc` swap (simplest for a throwaway
> proof) and runs in the already-built `mirage-valis:1.0.0`. It validates the *mechanism*
> (externalize → read back; halt → pickle → resume), which is independent of how the seam is wired.
> The real scripts use the explicit patched `EXTERNAL_TILE_HOOK` seam from Task 4 (spec §6 Option B).
> Run the spike inside the image, e.g.:
> `docker run --rm -v "$PWD":/work -w /work mirage-valis:1.0.0 python3 bin/spikes/spike_externalize_tiles.py`

- [ ] **Step 1: Generate test data**

Run: `python tests/testdata/generate_complete_testdata.py`
Expected: `tests/testdata/P001_*.ome.tiff` exist.

- [ ] **Step 2: Write the spike that forces tiling and captures a baseline**

Force `use_tiler=True` on small data by lowering the threshold, run unmodified VALIS once, save
the reference slide's stitched `bk_dxdy`:

```python
# bin/spikes/spike_externalize_tiles.py
import os, sys, pickle, numpy as np, pyvips
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "utils"))
from valis import registration, non_rigid_registrars as nrr
from valis import warp_tools

registration.TILER_THRESH_GB = 0  # force tiling on small test images

INPUT = "tests/testdata"; REF = "P001_ref.ome.tiff"
# Baseline: capture each tiler's stitched bk_dxdy by wrapping calc()
baseline = {}
orig_calc = nrr.NonRigidTileRegistrar.calc
def capture_calc(self, *a, **k):
    bk, fwd = orig_calc(self, *a, **k)
    baseline[id(self)] = warp_tools.vips2numpy(bk) if not isinstance(bk, np.ndarray) else np.array(bk)
    return bk, fwd
nrr.NonRigidTileRegistrar.calc = capture_calc
reg = registration.Valis(INPUT, "/tmp/spike_baseline", reference_img_f=REF,
                         non_rigid_registrar_cls=nrr.OpticalFlowWarper, max_non_rigid_registration_dim_px=512)
reg.register()
print("baseline tilers:", len(baseline))
pickle.dump(baseline, open("/tmp/spike_baseline.pkl", "wb"))
```

- [ ] **Step 3: Run the baseline spike**

Run: `python bin/spikes/spike_externalize_tiles.py`
Expected: prints `baseline tilers: N` (N ≥ number of moving slides), writes `/tmp/spike_baseline.pkl`.

- [ ] **Step 4: Add dump-mode + read-mode `calc` patches to the spike and assert equality**

Extend the spike: a **dump** patch that writes `self.moving_img/fixed_img/mask`, `self.expanded_bboxes`,
`self.tile_wh/tile_buffer`, `self.target_stats`, and the per-slide `processing_cls/kwargs` to a dir
then runs the *real* per-tile loop writing each tile field to disk; a **read** patch that skips
computation and loads tiles from disk before calling `warp_tools.stitch_tiles(...)` with the same
args. Assert the read-mode stitched field equals the baseline:

```python
def read_calc(self, *a, **k):
    tiles_bk = [pyvips.Image.new_from_file(f"/tmp/spike_tiles/bk_{i}.v") for i in range(self.n_tiles)]
    tiles_fwd = [pyvips.Image.new_from_file(f"/tmp/spike_tiles/fwd_{i}.v") for i in range(self.n_tiles)]
    bk = warp_tools.stitch_tiles(tiles_bk, self.expanded_bboxes, self.n_rows, self.n_cols, self.tile_buffer)
    fwd = warp_tools.stitch_tiles(tiles_fwd, self.expanded_bboxes, self.n_rows, self.n_cols, self.tile_buffer)
    return bk, fwd
# ... after a dump run produced /tmp/spike_tiles/*.v, run a read-mode Valis() and compare:
# assert np.array_equal(read_stitched_bk, baseline_bk)  # must be exact
```

- [ ] **Step 5: Run and confirm exact equality**

Run: `python bin/spikes/spike_externalize_tiles.py`
Expected: prints `BIT-IDENTICAL: True` (the read-mode stitched field exactly equals baseline).
**If False or pickle-resume fails:** STOP. Record the failure mode in the spec's §6 and switch to
the fallback (run rigid in PREP, full non-rigid+warp in FINALIZE from a re-derived registrar) —
do not proceed to Phase 1 until the resume mechanism is proven.

- [ ] **Step 5b: Validate the §5C halt → pickle → resume path (the default architecture)**

This is the decisive test for the per-step decomposition. Confirm that PREP can stop at the
non-rigid boundary, be pickled, and resumed in a *separate process* to produce the same result:

```python
# (a) PREP process: rigid only, dump inputs, RAISE before tile compute, pickle the registrar
#     install_halt_calc(dump_dir); try: reg.register() except TilesPending: pass
#     pickle.dump(reg, open("/tmp/spike_reg.pkl","wb"))
# (b) TILE step(s): compute each tile from dump_dir (no JVM) -> /tmp/spike_tiles/*.v
# (c) FINALIZE process (fresh python): reg = pickle.load(...); install_read_calc(tiles)
#     resume non-rigid -> assert stitched bk_dxdy == baseline_bk  # exact
print("PICKLE-RESUME OK:", resume_ok, " HALT-BIT-IDENTICAL:", np.array_equal(resumed_bk, baseline_bk))
```

Expected: `PICKLE-RESUME OK: True  HALT-BIT-IDENTICAL: True`.
**If the registrar is not pickle-safe at the halt point** (e.g. holds an open JVM/BioFormats
handle, or `register()` can't be resumed): record it, and the plan falls back to `install_dump_calc`
(PREP computes tiles inline) per Task 4's note — the per-step low-budget benefit is reduced but
correctness holds. Either way, do not proceed to Phase 1 until read-mode bit-identical (Step 5) is
proven.

- [ ] **Step 6: Write the decision record and remove the spike**

Append findings to the spec (a short "§6.1 Spike result" note: halt-vs-dump decision, whether the
registrar pickles/resumes cleanly, and the exact attributes that must be serialized).

```bash
git -C . rm -f bin/spikes/spike_externalize_tiles.py 2>/dev/null; rmdir bin/spikes 2>/dev/null || true
git add docs/superpowers/specs/2026-06-13-valis-tiled-parallel-registration-design.md
git commit -m ":white_check_mark: Spike: confirm Strategy-2 externalize-and-resume is bit-identical"
```

---

## Phase 1 — Pure, testable kernels (TDD)

### Task 2: Tile-grid manifest (`tile_grid.py`)

**Files:**
- Create: `bin/utils/tile_grid.py`
- Test: `tests/unit/test_tile_grid.py`

- [ ] **Step 1: Write the failing test (grid must equal VALIS exactly)**

```python
# tests/unit/test_tile_grid.py
import sys, os, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "bin", "utils"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))  # repo root for valis_lib
from tile_grid import build_grid
from valis_lib import warp_tools  # reference 1.0.0

def test_grid_matches_valis_exactly():
    shape_rc = (1700, 1300)
    tile_wh, tile_buffer = 512, 100
    grid = build_grid(shape_rc, tile_wh, tile_buffer)
    ref_bboxes = warp_tools.get_grid_bboxes(np.array(shape_rc), tile_wh, tile_wh, inclusive=True)
    ref_expanded = np.array([warp_tools.expand_bbox(b, tile_buffer, np.array(shape_rc)) for b in ref_bboxes])
    assert np.array_equal(np.array(grid["expanded_bboxes"]), ref_expanded)
    assert grid["n_rows"] == len(np.unique(ref_bboxes[:, 1]))
    assert grid["n_cols"] == len(np.unique(ref_bboxes[:, 0]))
    assert grid["n_tiles"] == len(ref_bboxes)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/test_tile_grid.py -v`
Expected: FAIL (ModuleNotFoundError: tile_grid).

- [ ] **Step 3: Implement `build_grid`**

```python
# bin/utils/tile_grid.py
"""Pure tile-grid computation, identical to VALIS 1.0.0 NonRigidTileRegistrar.register."""
import numpy as np
from valis import warp_tools

def build_grid(shape_rc, tile_wh=512, tile_buffer=100):
    shape = np.array(shape_rc)
    bboxes = warp_tools.get_grid_bboxes(shape, tile_wh, tile_wh, inclusive=True)
    expanded = np.array([warp_tools.expand_bbox(b, tile_buffer, shape) for b in bboxes])
    return {
        "shape_rc": [int(shape[0]), int(shape[1])],
        "tile_wh": tile_wh, "tile_buffer": tile_buffer,
        "expanded_bboxes": expanded.tolist(),
        "n_tiles": int(len(bboxes)),
        "n_cols": int(len(np.unique(bboxes[:, 0]))),
        "n_rows": int(len(np.unique(bboxes[:, 1]))),
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/unit/test_tile_grid.py -v`
Expected: PASS. (Note: test imports `valis_lib` as reference; runtime imports `valis` — both are 1.0.0.)

- [ ] **Step 5: Commit**

```bash
git add bin/utils/tile_grid.py tests/unit/test_tile_grid.py
git commit -m ":sparkles: Add tile-grid manifest matching VALIS 1.0.0 grid exactly"
```

### Task 3: Lossless tile field I/O (`tile_io.py`)

**Files:**
- Create: `bin/utils/tile_io.py`
- Test: `tests/unit/test_tile_io.py`

- [ ] **Step 1: Write the failing round-trip test**

```python
# tests/unit/test_tile_io.py
import sys, os, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "bin", "utils"))
from tile_io import write_field, read_field

def test_float32_roundtrip_is_lossless(tmp_path):
    rng = np.random.default_rng(0)
    field = rng.standard_normal((128, 96, 2)).astype(np.float32)  # (H,W,2) dx/dy
    p = str(tmp_path / "f.v")
    write_field(field, p)
    out = read_field(p)
    assert out.dtype == np.float32
    assert np.array_equal(out, field)  # exact, not approximate
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/test_tile_io.py -v`
Expected: FAIL (ModuleNotFoundError: tile_io).

- [ ] **Step 3: Implement lossless vips I/O**

```python
# bin/utils/tile_io.py
"""Lossless float32 displacement-field I/O via pyvips .v (native, uncompressed)."""
import numpy as np
from valis import warp_tools

def write_field(field_hw2, path):
    arr = np.ascontiguousarray(field_hw2.astype(np.float32))
    warp_tools.numpy2vips(arr).write_to_file(path)  # .v keeps raw float, no quantization

def read_field(path):
    import pyvips
    vi = pyvips.Image.new_from_file(path)
    return warp_tools.vips2numpy(vi).astype(np.float32)
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/unit/test_tile_io.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/utils/tile_io.py tests/unit/test_tile_io.py
git commit -m ":sparkles: Add lossless float32 tile-field I/O"
```

---

> ### ✅ Phase 1 DONE (2026-06-14) — Tasks 2 & 3 complete
> - `bin/utils/tile_grid.py` + `tests/unit/test_tile_grid.py` — grid matches VALIS 1.0.0 exactly (3/3).
> - `bin/utils/tile_io.py` + `tests/unit/test_tile_io.py` — lossless float32 `.v` round-trip (3/3).
> - Unit tests carry a stdlib `__main__` runner (pytest not installed in `mirage-valis:1.0.0`); run
>   them with `docker run --rm -v "$PWD":/work -w /work mirage-valis:1.0.0 python3 tests/unit/<f>.py`.

> ### 🔀 Phase 2 revised — Option A (plain-data handoff), decided 2026-06-14 (spec §6.2)
> The §6.1 spike killed the pickled-registrar handoff. Phase 2 now uses a **plain-data** PREP→FINALIZE
> contract (see spec §6.2 for the stage diagram + dump contract). Net changes to the tasks below:
> - **Before Task 5:** add **Task 4.5 — Option-A FINALIZE spike** (the next make-or-break): rebuild a
>   minimal `Slide` from plain dumped warp-state (M, processed/reg/aligned shapes, bg_color, crop,
>   src_f, slide_dimensions_wh, ref shapes/mask) + a disk-loaded stitched `dxdy`, call
>   `warp_and_save_slide`, and assert the output is **pixel-identical** to a full classic run. Only
>   after this passes, finalize Tasks 5/7.
> - **Task 4:** only the dump/halt hook is needed (FINALIZE stitches directly, never re-enters
>   `calc()`); halt is filesystem-signalled, not a caught exception.
> - **Task 5 (`reg_prep.py`):** dump plain warp-state + **processed 2-D** tiler inputs (process
>   fluorescence to its DAPI channel here, where `src_f` is live); `processing_cls=None` downstream;
>   **no** registrar pickle.
> - **Task 6 (`reg_tile.py`):** unchanged in spirit — runs on the pre-processed 2-D tile with
>   `processing_cls=None`.
> - **Task 7 (`reg_finalize.py`):** stitch + rebuild minimal Slide + `warp_and_save_slide`
>   (+ `register_micro` if on); no registrar reload.
> - **Task 10:** fluorescence parity must compare distributed vs **whole-image** classic (classic
>   tiling crashes on fluorescence, Blocker 3); brightfield may compare vs classic tiling directly.

## Phase 2 — Stage scripts

### Task 4: explicit `EXTERNAL_TILE_HOOK` seam (spec §6 Option B) + `valis_tiling.py`

The seam is a reviewable patch over the pip-installed VALIS (not runtime swizzle). It refactors
`NonRigidTileRegistrar.calc`'s body into `_calc_tiles` and dispatches through a default-`None`
class hook, so the classic path is byte-identical and our scripts just set the hook.

**Files:**
- Create: `containers/valis/calc_hook.patch`
- Modify: `containers/valis/Dockerfile` (apply the patch after `pip install`)
- Create: `bin/utils/valis_tiling.py`

- [ ] **Step 1: Generate the patch from the byte-identical `valis_lib/` reference**

```bash
# valis_lib/ == pip 1.0.0; edit a copy, diff it, that diff IS the patch the image applies.
cp valis_lib/non_rigid_registrars.py /tmp/nrr_orig.py
cp valis_lib/non_rigid_registrars.py /tmp/nrr_hooked.py
# In /tmp/nrr_hooked.py, inside `class NonRigidTileRegistrar(object):` add a class attr and split calc:
#   EXTERNAL_TILE_HOOK = None      # explicit external seam (default None = pristine classic path)
#   def calc(self, *args, **kwargs):
#       if NonRigidTileRegistrar.EXTERNAL_TILE_HOOK is not None:
#           return NonRigidTileRegistrar.EXTERNAL_TILE_HOOK(self)   # returns (bk_dxdy, fwd_dxdy)
#       return self._calc_tiles()
#   def _calc_tiles(self):        # <- original body of calc(), verbatim
#       ...original lines...
diff -u /tmp/nrr_orig.py /tmp/nrr_hooked.py > containers/valis/calc_hook.patch
```

Expected: `calc_hook.patch` is a small unified diff (≈ class attr + 4-line dispatch + indent of the
original body into `_calc_tiles`). **With the hook unset the classic path is byte-identical.**

- [ ] **Step 2: Apply the patch in the Dockerfile (after `pip install valis-wsi==1.0.0`)**

```dockerfile
# Add the explicit external tile-loop seam (default-off; classic path unchanged).
COPY calc_hook.patch /tmp/calc_hook.patch
RUN VALIS_DIR=$(python3 -c "import valis, os; print(os.path.dirname(valis.__file__))") && \
    patch "$VALIS_DIR/non_rigid_registrars.py" < /tmp/calc_hook.patch && \
    python3 -c "from valis.non_rigid_registrars import NonRigidTileRegistrar as T; assert hasattr(T,'EXTERNAL_TILE_HOOK') and T.EXTERNAL_TILE_HOOK is None; print('hook seam OK')"
```

- [ ] **Step 3: Rebuild and confirm the seam is present and default-off**

Run: `docker build -t mirage-valis:1.0.0 containers/valis/`
Expected: build prints `hook seam OK` (cached up to `pip install`; only the patch layer re-runs).

- [ ] **Step 4: Implement `valis_tiling.py` (sets/clears the hook — no method swizzle)**

```python
# bin/utils/valis_tiling.py
"""Set/clear NonRigidTileRegistrar.EXTERNAL_TILE_HOOK (the explicit seam patched into VALIS,
spec §6 Option B) to externalize the tile loop. The only module that knows the seam."""
import os, json, numpy as np
from valis import non_rigid_registrars as nrr, warp_tools

class TilesPending(Exception):
    """Raised by the halt hook after inputs are dumped, to stop PREP before tile compute."""

def _dump_inputs(self, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    self.moving_img.write_to_file(os.path.join(out_dir, "moving.v"))
    self.fixed_img.write_to_file(os.path.join(out_dir, "fixed.v"))
    if self.mask is not None:
        self.mask.write_to_file(os.path.join(out_dir, "mask.v"))
    if getattr(self, "target_stats", None) is not None:
        np.save(os.path.join(out_dir, "target_stats.npy"), np.asarray(self.target_stats))
    json.dump({
        "expanded_bboxes": np.asarray(self.expanded_bboxes).tolist(),
        "n_tiles": int(self.n_tiles), "n_rows": int(self.n_rows), "n_cols": int(self.n_cols),
        "tile_wh": int(self.tile_wh), "tile_buffer": int(self.tile_buffer),
        "has_mask": self.mask is not None,
    }, open(os.path.join(out_dir, "manifest.json"), "w"))

def install_halt_hook(out_dir):
    """§5C default: dump inputs then RAISE — PREP does NO tile compute on the fat JVM node."""
    def halt(self):
        _dump_inputs(self, out_dir)
        raise TilesPending(out_dir)
    nrr.NonRigidTileRegistrar.EXTERNAL_TILE_HOOK = halt

def install_dump_hook(out_dir):
    """Fallback: dump inputs, then run the real loop inline (PREP keeps a pickle-valid registrar)."""
    def dump(self):
        _dump_inputs(self, out_dir)
        return self._calc_tiles()   # the patched original loop
    nrr.NonRigidTileRegistrar.EXTERNAL_TILE_HOOK = dump

def install_read_hook(tiles_dir):
    """FINALIZE: load precomputed tiles and stitch via VALIS's own stitch_tiles."""
    import pyvips
    def read(self):
        bk = [pyvips.Image.new_from_file(os.path.join(tiles_dir, f"bk_{i}.v")) for i in range(self.n_tiles)]
        fwd = [pyvips.Image.new_from_file(os.path.join(tiles_dir, f"fwd_{i}.v")) for i in range(self.n_tiles)]
        return (warp_tools.stitch_tiles(bk,  self.expanded_bboxes, self.n_rows, self.n_cols, self.tile_buffer),
                warp_tools.stitch_tiles(fwd, self.expanded_bboxes, self.n_rows, self.n_cols, self.tile_buffer))
    nrr.NonRigidTileRegistrar.EXTERNAL_TILE_HOOK = read

def clear_hook():
    nrr.NonRigidTileRegistrar.EXTERNAL_TILE_HOOK = None
```

- [ ] **Step 5: Commit**

```bash
git add containers/valis/calc_hook.patch containers/valis/Dockerfile bin/utils/valis_tiling.py
git commit -m ":sparkles: Add explicit EXTERNAL_TILE_HOOK seam (patch) + hook-setting module"
```

> **Chosen split (spec §5C):** `install_halt_hook` is the **default** — PREP does rigid only and
> dumps tiler inputs, raising `TilesPending` so **no tile compute runs on the fat JVM node**; the
> no-JVM `REG_TILE` swarm does all tile compute. This requires the registrar pickled at the halt
> point be reloadable/resumable in `REG_FINALIZE` — **exactly what the Task 1 spike proves.**
> `install_dump_hook` (PREP computes tiles inline via `_calc_tiles`) is the **fallback** if
> halt/resume is not pickle-safe. Task 1 selects between them; the rest of the plan assumes halt.

### Task 5: `reg_prep.py`

**Files:**
- Create: `bin/reg_prep.py`

- [ ] **Step 1: Implement PREP (rigid only → dump inputs → halt → pickle)**

```python
#!/usr/bin/env python3
"""REG_PREP (spec §5C): VALIS RIGID registration only. Materialize per-slide non-rigid tiler
inputs + grid manifest, halt BEFORE any tile compute (so the heavy work goes to the no-JVM
REG_TILE swarm), and pickle the registrar for REG_FINALIZE. Distributed-tiling stage 1."""
import argparse, os, sys, pickle
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "utils"))
import valis_tiling
from valis import registration, feature_detectors, feature_matcher
from valis.non_rigid_registrars import OpticalFlowWarper

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--max-non-rigid-dim", type=int, default=4096)
    ap.add_argument("--max-processed-dim", type=int, default=2048)
    ap.add_argument("--jvm-heap-gb", type=int, default=32)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    dump_dir = os.path.join(args.out, "tiler_inputs")
    valis_tiling.install_halt_hook(dump_dir)  # §5C: sets EXTERNAL_TILE_HOOK → dump inputs then raise TilesPending
    registration.init_jvm(mem_gb=args.jvm_heap_gb)
    reg = registration.Valis(
        args.input_dir, args.out, reference_img_f=os.path.basename(args.reference),
        align_to_reference=True, crop="reference",
        non_rigid_registrar_cls=OpticalFlowWarper,
        feature_detector_cls=feature_detectors.SuperPointFD,
        matcher=feature_matcher.SuperGlueMatcher(),
        max_non_rigid_registration_dim_px=args.max_non_rigid_dim,
        max_processed_image_dim_px=args.max_processed_dim,
        create_masks=True,
    )
    try:
        reg.register()  # rigid runs; halt_calc raises once it reaches non-rigid tile compute
    except valis_tiling.TilesPending:
        pass  # expected: inputs dumped, registrar holds rigid state for FINALIZE to resume
    with open(os.path.join(args.out, "registrar.pkl"), "wb") as f:
        pickle.dump(reg, f)
    registration.kill_jvm()

if __name__ == "__main__":
    raise SystemExit(main())
```

> **Spike dependency:** this relies on the registrar being pickle-safe at the halt point and
> resumable in FINALIZE — Task 1 proves this. If the spike shows resume is not pickle-safe, switch
> `install_halt_hook` → `install_dump_hook` and have FINALIZE re-derive instead of resume (the §6
> Strategy-2 fallback); no other task changes.

- [ ] **Step 2: Commit**

```bash
git add bin/reg_prep.py
git commit -m ":sparkles: Add REG_PREP stage script (rigid only, halt before tile compute)"
```

> Multi-slide layout: dump under `tiler_inputs/<slide_name>/` keyed by `self` → slide name via the
> registrar's `slide_dict`; Task 1 confirms how to recover the slide name inside `calc`. The
> manifest per slide carries `expanded_bboxes`, `n_tiles`, grid dims.

### Task 6: `reg_tile.py`

**Files:**
- Create: `bin/reg_tile.py`

- [ ] **Step 1: Implement single-tile compute via VALIS `reg_tile`**

```python
#!/usr/bin/env python3
"""REG_TILE: compute ONE tile's bk/fwd displacement field using VALIS's own reg_tile logic.
Distributed-tiling stage 2 (fan-out)."""
import argparse, os, sys, json, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "utils"))
import numpy as np, pyvips
from valis.non_rigid_registrars import NonRigidTileRegistrar, OpticalFlowWarper

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs-dir", required=True)   # contains moving.v, fixed.v, mask.v?, manifest.json
    ap.add_argument("--tile-idx", type=int, required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--target-stats", default=None)  # path to .npy if present
    args = ap.parse_args()

    m = json.load(open(os.path.join(args.inputs_dir, "manifest.json")))
    reg = NonRigidTileRegistrar(tile_wh=m["tile_wh"], tile_buffer=m["tile_buffer"])
    reg.moving_img = pyvips.Image.new_from_file(os.path.join(args.inputs_dir, "moving.v"))
    reg.fixed_img = pyvips.Image.new_from_file(os.path.join(args.inputs_dir, "fixed.v"))
    reg.mask = pyvips.Image.new_from_file(os.path.join(args.inputs_dir, "mask.v")) if m["has_mask"] else None
    reg.expanded_bboxes = np.array(m["expanded_bboxes"])
    reg.n_tiles = m["n_tiles"]; reg.n_rows = m["n_rows"]; reg.n_cols = m["n_cols"]
    reg.bk_dxdy_tiles = [None] * reg.n_tiles
    reg.fwd_dxdy_tiles = [None] * reg.n_tiles
    reg.non_rigid_registrar_cls = OpticalFlowWarper
    reg.processing_cls = None; reg.processing_kwargs = None
    reg.target_stats = np.load(args.target_stats) if args.target_stats else None
    import tqdm; reg.pbar = tqdm.tqdm(total=1)

    reg.reg_tile(args.tile_idx, threading.Lock())  # VALIS's exact per-tile kernel

    os.makedirs(args.out_dir, exist_ok=True)
    reg.bk_dxdy_tiles[args.tile_idx].write_to_file(os.path.join(args.out_dir, f"bk_{args.tile_idx}.v"))
    reg.fwd_dxdy_tiles[args.tile_idx].write_to_file(os.path.join(args.out_dir, f"fwd_{args.tile_idx}.v"))

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Commit**

```bash
git add bin/reg_tile.py
git commit -m ":sparkles: Add REG_TILE stage script (single-tile VALIS reg_tile)"
```

### Task 7: `reg_finalize.py`

**Files:**
- Create: `bin/reg_finalize.py`

- [ ] **Step 1: Implement FINALIZE (read-mode calc + compose + warp)**

```python
#!/usr/bin/env python3
"""REG_FINALIZE: reload the registrar, inject precomputed tiles into the tiler via read-mode
calc(), then run VALIS's own stitch + composition + warp_and_save_slide. Distributed stage 3."""
import argparse, os, sys, pickle, glob
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "utils"))
import valis_tiling
from valis import registration

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registrar", required=True)     # registrar.pkl from PREP
    ap.add_argument("--tiles-dir", required=True)      # all bk_*.v / fwd_*.v from REG_TILE
    ap.add_argument("--src-slide", required=True)      # full-res source ome.tiff
    ap.add_argument("--out", required=True)
    ap.add_argument("--jvm-heap-gb", type=int, default=32)
    args = ap.parse_args()

    valis_tiling.install_read_hook(args.tiles_dir)  # sets EXTERNAL_TILE_HOOK → load tiles + stitch
    registration.init_jvm(mem_gb=args.jvm_heap_gb)
    reg = pickle.load(open(args.registrar, "rb"))
    # Re-run only the non-rigid stage so read-mode calc() feeds the precomputed tiles, then warp.
    # (Exact entrypoint confirmed by Task 1: either reg.register_non_rigid(...) or the slide loop.)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    slide_name = os.path.basename(args.src_slide).split(".ome")[0]
    slide_obj = reg.slide_dict[slide_name]
    slide_obj.warp_and_save_slide(src_f=args.src_slide, dst_f=args.out, level=0,
                                  non_rigid=True, crop=True, interp_method="bilinear")
    registration.kill_jvm()

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Commit**

```bash
git add bin/reg_finalize.py
git commit -m ":sparkles: Add REG_FINALIZE stage script (inject tiles, stitch, warp)"
```

> The exact re-entry point that triggers read-mode `calc()` (full `register_non_rigid` vs the
> per-slide `calc_deformation`) is locked by Task 1's spike; wire whichever the spike proved
> produces a bit-identical stitched field.

> **Micro-registration (spec §5A):** when `--skip-micro-registration` is NOT set, `reg_finalize.py`
> must, after injecting the first-pass tiles, call `registrar.register_micro(..., tile_wh=2048,
> max_non_rigid_registration_dim_px=floor(min_max_size*micro_reg_fraction))` in-process (Option 1)
> BEFORE `warp_and_save_slide` — otherwise the distributed path silently drops the second non-rigid
> pass and diverges from classic-with-micro. Add `--skip-micro-registration` / `--micro-reg-fraction`
> args mirroring `bin/register.py`. (Option 2 — distributing micro as a second wave — is a later
> enhancement.)

---

## Phase 3 — Nextflow wiring

### Task 8: Processes `REG_PREP`, `REG_TILE`, `REG_FINALIZE`

**Files:**
- Create: `modules/local/reg_prep.nf`, `modules/local/reg_tile.nf`, `modules/local/reg_finalize.nf`
- Create: `tests/modules/reg_prep.nf.test`, `tests/modules/reg_tile.nf.test`, `tests/modules/reg_finalize.nf.test`

- [ ] **Step 1: Write `reg_prep.nf` (mirror `register.nf` header/label/container/stub conventions)**

```groovy
// modules/local/reg_prep.nf — distributed-tiling stage 1
process REG_PREP {
    tag "${patient_id}"
    label 'process_high'
    container "${params.container_registry}/valis:${params.container_tag}"

    input:
    tuple val(meta), val(patient_id), path(reference, stageAs: 'ref/*'), path(preproc_files, stageAs: 'input_?/*'), val(all_metas)

    output:
    tuple val(patient_id), path("prep/registrar.pkl"), path("prep/tiler_inputs"), val(all_metas), emit: prepped
    tuple val(patient_id), path("prep/tiler_inputs/**/manifest.json"),                emit: manifests
    path "versions.yml", emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    def max_nr = params.reg_max_non_rigid_dim ?: 4096
    """
    mkdir -p prep
    reg_prep.py --input-dir . --out prep --reference ${reference.name} --max-non-rigid-dim ${max_nr} --jvm-heap-gb ${Math.min(params.reg_jvm_heap_gb ?: 32, task.memory.toGiga() - 4)}
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        valis: \$(python -c "import valis; print(valis.__version__)" 2>/dev/null || echo unknown)
    END_VERSIONS
    """

    stub:
    """
    mkdir -p prep/tiler_inputs/slideA
    touch prep/registrar.pkl
    echo '{"expanded_bboxes":[[0,0,10,10]],"n_tiles":1,"n_rows":1,"n_cols":1,"tile_wh":512,"tile_buffer":100,"has_mask":false}' > prep/tiler_inputs/slideA/manifest.json
    echo '"${task.process}":' > versions.yml; echo '    valis: stub' >> versions.yml
    """
}
```

- [ ] **Step 2: Write `reg_tile.nf` (the fan-out unit)**

```groovy
// modules/local/reg_tile.nf — distributed-tiling stage 2 (one task per tile)
process REG_TILE {
    tag "${patient_id}:${slide}:${tile_idx}"
    label 'process_low'
    container "${params.container_registry}/valis:${params.container_tag}"

    input:
    tuple val(patient_id), val(slide), path(inputs_dir), val(tile_idx)

    output:
    tuple val(patient_id), val(slide), path("tiles/*.v"), emit: tiles
    path "versions.yml", emit: versions

    script:
    def ts = "${inputs_dir}/target_stats.npy"
    """
    mkdir -p tiles
    reg_tile.py --inputs-dir ${inputs_dir} --tile-idx ${tile_idx} --out-dir tiles \\
        \$( [ -f ${ts} ] && echo "--target-stats ${ts}" )
    echo '"${task.process}":' > versions.yml; echo '    valis: '\$(python -c "import valis;print(valis.__version__)") >> versions.yml
    """

    stub:
    """
    mkdir -p tiles; touch tiles/bk_${tile_idx}.v tiles/fwd_${tile_idx}.v
    echo '"${task.process}":' > versions.yml; echo '    valis: stub' >> versions.yml
    """
}
```

- [ ] **Step 3: Write `reg_finalize.nf` (fan-in)**

```groovy
// modules/local/reg_finalize.nf — distributed-tiling stage 3
process REG_FINALIZE {
    tag "${patient_id}:${slide}"
    label 'process_high'
    container "${params.container_registry}/valis:${params.container_tag}"

    input:
    tuple val(patient_id), val(slide), path(registrar), path(tiles, stageAs: 'tiles/*'), path(src_slide)

    output:
    tuple val(patient_id), path("registered_slides/*_registered.ome.tiff"), emit: registered
    path "versions.yml", emit: versions

    script:
    """
    mkdir -p registered_slides
    reg_finalize.py --registrar ${registrar} --tiles-dir tiles --src-slide ${src_slide} \\
        --out registered_slides/${slide}_registered.ome.tiff \\
        --jvm-heap-gb ${Math.min(params.reg_jvm_heap_gb ?: 32, task.memory.toGiga() - 4)}
    echo '"${task.process}":' > versions.yml; echo '    valis: '\$(python -c "import valis;print(valis.__version__)") >> versions.yml
    """

    stub:
    """
    mkdir -p registered_slides; touch registered_slides/${slide}_registered.ome.tiff
    echo '"${task.process}":' > versions.yml; echo '    valis: stub' >> versions.yml
    """
}
```

- [ ] **Step 4: Write nf-test stubs (mirror `tests/modules/register.nf.test`)**

```groovy
// tests/modules/reg_prep.nf.test
nextflow_process {
    name "Test REG_PREP process"
    script "modules/local/reg_prep.nf"
    process "REG_PREP"
    tag "modules"; tag "modules_local"; tag "reg_prep"
    test("REG_PREP stub") {
        options "-stub"
        when { process { """
            input[0] = [ [patient_id:'P001'], 'P001',
                file('\$projectDir/tests/testdata/P001_ref.ome.tiff', checkIfExists: true),
                [ file('\$projectDir/tests/testdata/P001_mov1.ome.tiff', checkIfExists: true) ],
                [ [patient_id:'P001', is_reference:true, channels:['DAPI']] ] ]
        """ } }
        then { assert process.success }
    }
}
```
(Repeat the same shape for `reg_tile.nf.test` and `reg_finalize.nf.test` with their stub inputs.)

- [ ] **Step 5: Run the stub tests**

Run: `nf-test test tests/modules/reg_prep.nf.test --profile test,docker --verbose`
Expected: PASS (stub).

- [ ] **Step 6: Commit**

```bash
git add modules/local/reg_prep.nf modules/local/reg_tile.nf modules/local/reg_finalize.nf tests/modules/reg_*.nf.test
git commit -m ":sparkles: Add REG_PREP/REG_TILE/REG_FINALIZE processes + stub tests"
```

### Task 9: Subworkflow routing + params + schema

**Files:**
- Modify: `subworkflows/local/registration.nf`
- Modify: `nextflow.config:97-109` (reg_ block)
- Modify: `nextflow_schema.json`

- [ ] **Step 1: Add params to `nextflow.config` reg_ block**

```groovy
    // Distributed tiled non-rigid registration (opt-in; bit-identical to classic in tiling regime)
    reg_distributed_tiling     = false
    reg_dist_sub_threshold     = 'auto'   // 'auto' = single whole-image tile below threshold; 'force' = always tile
    reg_dist_tiles_per_task    = 1        // tiles per REG_TILE task (scheduling only; no output effect)
    reg_max_non_rigid_dim      = 4096
```

- [ ] **Step 2: Add the routing branch in `registration.nf`**

Add near the existing `REGISTER` call (guarded so default path is unchanged):

```groovy
include { REG_PREP }     from '../../modules/local/reg_prep'
include { REG_TILE }     from '../../modules/local/reg_tile'
include { REG_FINALIZE } from '../../modules/local/reg_finalize'

if ( params.reg_distributed_tiling ) {
    if ( params.reg_non_rigid_backend && params.reg_non_rigid_backend != 'optical_flow' )
        error "reg_distributed_tiling requires the OpticalFlowWarper backend (bit-identical precondition); got ${params.reg_non_rigid_backend}"
    REG_PREP( ch_register_in )
    // fan-out: read each slide's manifest -> emit one (patient, slide, inputs_dir, tile_idx) per tile
    ch_tiles = REG_PREP.out.prepped
        .flatMap { pid, pkl, inputs, metas ->
            def manifests = file("${inputs}/**/manifest.json")
            manifests.collectMany { mf ->
                def m = new groovy.json.JsonSlurper().parse(mf.toFile())
                def slide = mf.parent.name
                (0..<m.n_tiles).collect { i -> tuple(pid, slide, mf.parent, i) }
            }
        }
    REG_TILE( ch_tiles )
    ch_finalize = REG_TILE.out.tiles
        .groupTuple(by: [0, 1])           // per (patient, slide)
        .join( REG_PREP.out.prepped.map { pid, pkl, inputs, metas -> tuple(pid, pkl) }, by: 0 )
        // ... join with full-res source slide path; build REG_FINALIZE input tuple
    REG_FINALIZE( ch_finalize )
    ch_registered = REG_FINALIZE.out.registered.groupTuple(by: 0)
} else {
    REGISTER( ch_register_in )
    ch_registered = REGISTER.out.registered
}
```

- [ ] **Step 3: Add the params to `nextflow_schema.json`** (mirror an existing `reg_*` entry's type/description block for each new key).

- [ ] **Step 4: Run a full stub pipeline both ways**

Run: `nextflow run . -profile test,docker -stub --outdir results_classic`
Run: `nextflow run . -profile test,docker -stub --outdir results_dist --reg_distributed_tiling true`
Expected: both complete; distributed run shows `REG_PREP`/`REG_TILE`/`REG_FINALIZE` tasks.

- [ ] **Step 5: Commit**

```bash
git add subworkflows/local/registration.nf nextflow.config nextflow_schema.json
git commit -m ":sparkles: Route registration through distributed tiling when reg_distributed_tiling=true"
```

---

## Phase 4 — Verification & hardening

### Task 10: Bit-identical verification + large fixture

**Files:**
- Create: `tests/integration/test_bit_identical.py`
- Create: `tests/testdata/generate_large_fixture.py` (upscales test slides past `TILER_THRESH_GB`)

- [ ] **Step 1: Write the large-fixture generator** (upscale a test slide so the est_GB formula in §5 exceeds 10 GB at the configured non-rigid dim, forcing classic VALIS to tile).

- [ ] **Step 2: Write the verification test (exact equality)**

```python
# tests/integration/test_bit_identical.py
import subprocess, numpy as np, tifffile, glob, os
def test_distributed_equals_classic(tmp_path):
    # run classic
    subprocess.run(["nextflow","run",".","-profile","test_large,docker","--outdir",str(tmp_path/"c")], check=True)
    # run distributed
    subprocess.run(["nextflow","run",".","-profile","test_large,docker","--reg_distributed_tiling","true","--outdir",str(tmp_path/"d")], check=True)
    for cf in glob.glob(str(tmp_path/"c"/"**"/"*_registered.ome.tiff"), recursive=True):
        df = cf.replace("/c/", "/d/")
        a = tifffile.imread(cf); b = tifffile.imread(df)
        assert np.array_equal(a, b), f"pixel mismatch: {os.path.basename(cf)}"
```

- [ ] **Step 3: Run the verification (the acceptance gate)**

Run: `pytest tests/integration/test_bit_identical.py -v`
Expected: PASS — distributed output is pixel-identical to classic on the large fixture.

- [ ] **Step 4: Granularity invariance** — re-run distributed with `--reg_dist_tiles_per_task 4` and assert identical to `=1`. Add as a second assertion/test.

- [ ] **Step 4b: Micro-registration ON parity (spec §5A)** — re-run both classic and distributed with `--skip_micro_registration false` on the large fixture; assert pixel-identical. Confirms `reg_finalize.py` reproduces `register_micro()` rather than dropping it.

- [ ] **Step 5: Document** (Dockerfile pin `==1.0.0` + `calc_hook.patch` are already done — Task 4 / build).

Add a "Distributed tiled registration" section to `docs/registration_methods.md` documenting the
toggle, the regime boundary (`est_GB>10`), the DeepFlow-only precondition, `tiles_per_task`,
micro-registration handling (§5A), and the low-budget per-step resource sizing (§5C).

- [ ] **Step 6: Commit**

```bash
git add tests/integration/test_bit_identical.py tests/testdata/generate_large_fixture.py docs/registration_methods.md
git commit -m ":white_check_mark: Bit-identical verification harness + docs"
```

---

## Self-Review notes

- **Spec coverage:** §4 architecture → Tasks 5-9; §5 fidelity preconditions → Task 1 (mechanism),
  Task 3 (lossless), Task 4 (reuse stitch_tiles), Task 9 (DeepFlow guard); §8 params → Task 9;
  §9 verification plan → Task 10; R3 (pin) → Task 10.5; R9 (backend guard) → Task 9.2.
- **Known deferred-to-spike detail:** the exact VALIS re-entry point in `reg_finalize.py` and the
  PREP halt-vs-recompute choice are locked by Task 1 before Phase 2 code is finalized. This is by
  design (de-risk first), not a placeholder.
- **Type consistency:** `build_grid` keys (`expanded_bboxes`, `n_tiles`, `n_rows`, `n_cols`,
  `tile_wh`, `tile_buffer`) are reused verbatim by `valis_tiling.py`, `reg_tile.py`, and the
  manifest consumers in `registration.nf`.
