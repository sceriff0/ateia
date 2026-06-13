# Design: Nextflow-Parallel VALIS Tiled Non-Rigid Registration

- **Date:** 2026-06-13
- **Branch / worktree:** `valis-tiled-parallel`
- **Status:** Design spec (implementation deferred to a separate plan)
- **Author:** brainstormed with Claude Code
- **Reference VALIS version:** `valis-wsi == 1.0.0` (PyPI sdist; **byte-identical** to the repo's `valis_lib/` across all algorithm files — verified 2026-06-13)

---

## 1. Goal

Add an **opt-in** registration path that takes VALIS's *internal, in-process* tiled
non-rigid registration and lifts the per-tile loop up into **Nextflow processes**, so that:

1. **Throughput** — independent tiles are distributed across cluster nodes instead of
   threads on one node.
2. **Peak RAM** — each task holds only one tile (+ buffer) of the non-rigid displacement
   field, instead of the whole image's field on a single node.

The classic single-process path (`REGISTER`) **remains the default and is unchanged**.
The new path is selected by a Nextflow parameter (see §8). Output of the new path must be
**bit-identical** to classic VALIS *in the regime where classic VALIS itself tiles* (see §5).

### Non-goals (this session / this spec)

- No implementation — this document is the design + parameter set only.
- No change to the rigid / feature-matching / micro-registration stages' *algorithms*
  (they are reused verbatim; only their orchestration changes).
- No attempt to make the *whole-image* (sub-threshold) optical-flow path tiled-identical
  (it is mathematically not reproducible by tiling — see §5).

---

## 2. Reference pinning — what "classic VALIS" means here

The registration container (`containers/valis/Dockerfile`) installs VALIS via an
**unpinned** `pip install valis-wsi`. The image in use resolved to **1.0.0** at build time.
The pipeline's `bin/register.py` imports `from valis import registration`, i.e. the **pip
site-packages copy** (1.0.0), *not* the repo's `valis_lib/` directory (Nextflow never stages
`valis_lib/` into the task workdir).

> **Therefore "classic VALIS" = `valis-wsi 1.0.0`'s `NonRigidTileRegistrar`.**
> The repo's `valis_lib/` is a copy of **1.0.0** (`__version__ == "1.0.0"`) and is
> **byte-identical to the pristine PyPI 1.0.0 sdist** across every algorithm file
> (`non_rigid_registrars.py`, `warp_tools.py`, `registration.py`, `serial_non_rigid.py`,
> `feature_detectors.py`, `feature_matcher.py` — verified by `diff`). So `valis_lib/` **is** a
> valid local reading/reference copy, and `REG_TILE` may even vendor it to guarantee the
> kernel matches the image exactly.
>
> **Do not** use upstream 1.2.0 as the reference — its tile kernel differs (1.2.0 normalizes
> tiles with `collect_img_stats([fixed, moving])`; 1.0.0 uses tile-local masked
> `get_channel_stats`), which would produce a different baseline.

### Recommended hardening (separate, small change)

Pin the container: `pip install valis-wsi==1.0.0`. The current unpinned install means a future
image rebuild would silently jump to a newer VALIS (e.g. 1.2.0), changing the "classic"
baseline and breaking bit-identical verification.

---

## 3. The classic tiled algorithm (`NonRigidTileRegistrar`, 1.0.0)

After rigid alignment, for each moving slide downsampled to
`max_non_rigid_registration_dim_px`, VALIS computes the non-rigid displacement field like so
(`valis/non_rigid_registrars.py`, `valis/warp_tools.py`):

1. **Grid** — `warp_tools.get_grid_bboxes(shape_rc, tile_wh, tile_wh, inclusive=True)`
   → regular `n_rows × n_cols` grid of `tile_wh`-sized bboxes (default **512**).
2. **Expand** — each bbox grown by `tile_buffer` overlap via
   `warp_tools.expand_bbox(bbox, tile_buffer, shape_rc)` (default buffer **100**).
3. **Per-tile registration** (the parallel kernel — currently `joblib`/`pqdm` over threads,
   one call to `reg_tile(i)` per tile):
   - `extract_area` the moving + fixed (+ mask) tile from the pyvips images.
   - Grayscale / process the tile (1.0.0 uses a single `processing_cls`, else
     `np.abs(1 - rgb2gray)`), then `norm_tiles(...)` — **tile-local** intensity stats via
     `preprocessing.get_channel_stats` over the masked tile pixels, falling back to the global
     `self.target_stats` on `ValueError`. (The per-tile thread loop uses `joblib.Parallel`
     with the `threading` backend in 1.0.0.)
   - `OpticalFlowWarper().register(moving_normed, fixed_normed)` → per-tile **backward**
     displacement `bk_dxdy`; `fwd_dxdy = warp_tools.get_inverse_field(bk_dxdy)`.
   - Empty tiles (constant / fully masked) → zero displacement field, skipped.
4. **Stitch** — `warp_tools.stitch_tiles(tiles, expanded_bboxes, n_rows, n_cols, tile_buffer)`
   merges per-tile fields into one full-size `bk_dxdy` / `fwd_dxdy`. **This is not index
   placement** — it uses pyvips `merge(..., mblend=overlap)`, i.e. **linear blending across the
   `tile_buffer`-pixel seam**, so overlap regions are a weighted mix of *adjacent* tiles
   (`warp_tools.py:550,564`). The merge order is fixed (rows left→right, then top→bottom),
   independent of tile-completion order, so it stays deterministic — but bit-identical
   externalization requires reusing **this exact function** (see §5).
5. **Compose & warp** — `serial_non_rigid` composes the stitched field with the rigid
   transform (and, in `align_to_reference` mode, the reference's field), then
   `warp_and_save_slide` applies the full field to the full-resolution slide.

**Why this parallelizes cleanly (verified 2026-06-13 by adversarial review of the 1.0.0 source):**
step 3 is side-effect-free per tile — `reg_tile` writes only its own indexed slots
`bk_dxdy_tiles[i]`/`fwd_dxdy_tiles[i]`, instantiates a fresh registrar per tile, and never
mutates shared state (`non_rigid_registrars.py:1279-1353`). The `multiprocessing.Lock` in
`calc()` is purely a **pyvips I/O guard** (libvips isn't thread-safe with cache disabled) — it
has **no effect on numeric results**, so external processes produce the same per-tile arrays.
DeepFlow and `get_inverse_field` (fixed 10 iterations) are deterministic with no RNG.

**Three preconditions for bit-identical externalization (all confirmed necessary):**
1. **DeepFlow/`OpticalFlowWarper` backend only.** The `SimpleElastixWarper` backend samples
   with `RandomCoordinate`/`np.random` per tile (`non_rigid_registrars.py:685-691,801`); separate
   processes reseed independently → **not reproducible**. Distributed mode must hard-require the
   `OpticalFlowWarper` backend (the pipeline default — see `register.py:567`).
2. **Lossless float32 round-trip.** Per-tile fields are in-memory `float32` pyvips images; the
   on-disk format between `REG_TILE` and stitch must not quantize or compress lossily.
3. **Reuse VALIS's own `stitch_tiles`** on the **identical** `expanded_bboxes`/`n_rows`/`n_cols`
   grid — because the seam is `mblend`-blended (§3 step 4), a re-implemented stitch would diverge.

---

## 4. Architecture — Approach A (per-tile fan-out)

Four Nextflow processes replace the monolithic `REGISTER`, selected only when the toggle is on:

```
                                   ┌──────────────────────────────────────────┐
  per patient                      │  classic REGISTER (default, unchanged)     │
  ──────────────►  toggle off ───► │  single process, VALIS internal threading  │
                                   └──────────────────────────────────────────┘
       │ toggle on
       ▼
┌────────────────┐   per slide   ┌────────────────┐  fan-out: 1 task / tile  ┌────────────────┐
│  REG_PREP      │──────────────►│ (tile manifest)│─────────────────────────►│  REG_TILE      │
│  rigid align + │  moving.v,    │  bbox_i, cfg   │                          │  extract tile, │
│  downsample +  │  fixed.v,     └────────────────┘                          │  DeepFlow,     │
│  grid bboxes   │  mask.v,                                                   │  emit dxdy_i   │
└────────────────┘  bboxes.json                                              └───────┬────────┘
                                                                                     │ fan-in (groupTuple by slide)
                                                                                     ▼
                                                              ┌────────────────┐  ┌────────────────┐
                                                              │  REG_WARP      │◄─│  REG_STITCH    │
                                                              │  apply full    │  │ stitch_tiles → │
                                                              │  field to L0,  │  │ full bk/fwd    │
                                                              │  save ome.tiff │  │ + compose      │
                                                              └────────────────┘  └────────────────┘
```

| Process | Granularity | Reads | Emits | Label |
|---|---|---|---|---|
| `REG_PREP` | 1 / moving slide | preprocessed slides + reference | `moving.v`, `fixed.v`, `mask.v` (tiled pyramidal), `bboxes.json`, pickled registrar state | `process_high` (loads full image once to downsample) |
| `REG_TILE` | **1 / tile** | `moving.v`+`fixed.v`+`mask.v` (lazy `extract_area` of one bbox), `bboxes.json[i]` | `dxdy_<slide>_<i>.v` (bk+fwd) | `process_low` (one tile in RAM) |
| `REG_STITCH` | 1 / slide (fan-in) | all `dxdy_<slide>_*.v` + `bboxes.json` | full `bk_dxdy.v`, `fwd_dxdy.v` | `process_medium` |
| `REG_WARP` | 1 / slide | full field + full-res source slide | `*_registered.ome.tiff` | `process_high` (full-res I/O) |

`REG_TILE` is the fan-out unit. Nextflow's `groupTuple(by: slide_id, size: n_tiles)` re-collects
tiles for `REG_STITCH`; `n_tiles` is known from `REG_PREP`'s manifest, enabling **streaming**
group-by (consistent with the repo's existing streaming `groupTuple` pattern — see
`CsvUtils.countImagesPerPatient`).

> The reference slide produces an identity (zero) field and skips `REG_TILE` entirely.

---

## 5. Fidelity — the regime boundary (read this before claiming "bit-identical")

VALIS tiles non-rigid registration **only** when estimated memory exceeds the threshold
(`registration.py:3455-3464`): `use_tiler = (img_gb + displacement_gb + processed_img_gb) > TILER_THRESH_GB (10)`
— a **hard strict `>`** (exactly 10 GB → not tiled). Each term is multiplied by the slide
count `self.size`, and `img_gb` uses the reference slide's full channel count and native dtype
(it is a *stack* estimate, not a single-image cost). `REG_PREP` must compute this same estimate
to decide auto-fallback (§5a).

- **Above threshold (big images):** classic VALIS tiles with `tile_wh=512`, `tile_buffer=100`.
  Our distributed path reproduces *exactly this* tile grid + kernel + stitch → **bit-identical**.
- **Below threshold (small images):** classic VALIS runs DeepFlow on the **whole image** (no
  tiles). A tiled run **cannot** reproduce this — seams/blending differ. There is no set of
  tile parameters that makes tiling equal whole-image optical flow.

### Consequence for the toggle (must be explicit)

The distributed mode is **for the tiling regime**. Two honest options for sub-threshold images
(decision deferred to implementation, default = option (a)):

- **(a) Auto-fallback (recommended default):** if `REG_PREP` computes `est_GB ≤ threshold`,
  emit a single full-image "tile" and let one `REG_TILE` task run whole-image DeepFlow — which
  *is* bit-identical to classic's whole-image path. Distribution kicks in only when classic
  would tile. This makes the toggle a strict superset of classic behavior.
- **(b) Force-tile both sides:** lower the effective threshold so classic *also* tiles, making
  both paths share the tiled reference. Changes classic output for small images — only for users
  who explicitly want uniform tiling. Off by default.

### Other fidelity-critical details to replicate verbatim

1. **Reuse VALIS functions, do not reimplement.** `REG_TILE` instantiates
   `valis.non_rigid_registrars.NonRigidTileRegistrar`, sets its attributes
   (`moving_img`, `fixed_img`, `mask`, `expanded_bboxes`, processing config, `target_stats`),
   and calls its own `reg_tile(i, lock)` for a single index. `REG_STITCH` calls
   `warp_tools.stitch_tiles(...)`. Fidelity is then guaranteed *by construction*.
2. **Identical grid.** `REG_PREP` must call the same `get_grid_bboxes(..., inclusive=True)` and
   `expand_bbox(..., tile_buffer, shape_rc)` so tile indices/bboxes match 1.0.0 exactly.
3. **Per-slide inputs `REG_PREP` must reproduce and ship to every `REG_TILE`** (all confirmed
   consumed in 1.0.0):
   - **`target_stats`** — non-None by **default** (`norm_method="img_stats"`,
     `registration.py:54,1684`); shipped as `NR_STATS_KEY` whenever `norm_method` is set, and
     consumed by `norm_tiles`'s `ValueError` fallback. Must be reproduced.
   - **`processing_cls` / `processing_kwargs`** — **per-slide** (`registration.py:3319,3327-3328`),
     applied per tile in `process_tile`. Must be reproduced exactly.
   - **`mask`** — **per-slide**, derived deterministically from `slide_obj.rigid_reg_mask`
     warped through the rigid `M` (`registration.py:3497-3511`) → reproducible from the rigid stage.
   - **`tile_buffer=100`** — comes from the **`NonRigidTileRegistrar` constructor default**, *not*
     from `get_nr_tiling_params` (which only sets `tile_wh=512`). Reproduce both explicitly.
   - **The moving image** fed to the tiler is the source pyramid level resized to
     `max_non_rigid_registration_dim_px` then **warped through the rigid `M`**
     (`registration.py:3488-3553`); on the first non-rigid pass `dxdy=None` (rigid-only).
     `REG_PREP` must materialize exactly this image.
4. **Displacement composition.** The stitched `bk_dxdy` must be composed with the rigid
   transform / reference field **exactly** as `serial_non_rigid` does
   (`bk_dxdy_from_ref = bk_dxdy + moving_bk_dxdy`, `remove_invasive_displacements`,
   `get_inverse_field`). **Strongly prefer keeping this composition inside a VALIS call**
   (see §6, Strategy 2) rather than hand-porting it.
5. **`get_inverse_field(n_inter=10)`** default must be preserved when inverting fields.
6. **dtype/precision.** Tile fields are `float32` pyvips images (`numpy2vips(np.dstack(...).astype(np.float32))`).
   Round-tripping through disk must stay `float32` (lossless container, e.g. uncompressed/LZW
   `.v` or `.tif`), never quantize.

---

## 5A. Micro-registration (`skip_micro_registration=false`) — IMPORTANT

The pipeline default is `skip_micro_registration=true`, so the base design (which runs only
rigid + the **first** non-rigid pass) is bit-identical to classic by default. **When micro is
ON, the distributed path must also reproduce `register_micro()` or it silently diverges.**
Verified against `registration.py:4170-4279` and `bin/register.py`:

- `register_micro()` is a **second non-rigid pass at higher resolution**. It calls
  `prep_images_for_large_non_rigid_registration(max_img_dim=micro_reg_size,
  updating_non_rigid=True, ...)` (`registration.py:4251-4255`).
- **`updating_non_rigid=True`** ⇒ the moving image is warped through rigid `M` **plus the
  first pass's stitched `dxdy`**; the micro pass computes the *residual* and composes it
  additively onto the existing field. This is a **hard sequential dependency** on wave 1.
- `using_tiler` is decided the same way (`est_GB > TILER_THRESH_GB`) at the micro resolution
  (`registration.py:4260`); if tiling, it uses `NonRigidTileRegistrar` with **`tile_wh=2048`**
  (`bin/register.py` passes `tile_wh=2048`; the main pass uses 512). It then runs through the
  **same `serial_non_rigid.register_images` → `NonRigidTileRegistrar.calc()` path** — so the
  same Strategy-2 monkeypatch externalizes it.
- `micro_reg_size = floor(min_max_size × micro_reg_fraction)` (default fraction `0.125`,
  `bin/register.py`). This may fall **below** the tiler threshold, in which case micro runs
  whole-image (no tiles to distribute) → Option 1 below is the only sensible path.

### Two ways to support micro (decision for the plan; default = Option 1)

- **Option 1 — micro in-process inside `REG_FINALIZE` (recommended first cut).** Strategy 2
  keeps a live VALIS registrar in `REG_FINALIZE`; after injecting the distributed first-pass
  tiles it calls `registrar.register_micro(..., tile_wh=2048)` normally (VALIS's own threaded
  tiling). **Bit-identical** (micro untouched), minimal plumbing. Downside: micro's big-image
  cost stays on one node — and micro is often the *heavier* pass.
- **Option 2 — distribute micro as a second wave.** `REG_PREP_MICRO` (updating mode) →
  `REG_TILE_MICRO` (`tile_wh=2048`) → `REG_STITCH_MICRO`, then warp. Full RAM/throughput win on
  both passes; costs a wave-1→wave-2 barrier and more plumbing.

```
rigid → [WAVE 1: PREP → TILE×N @512 → STITCH] → first-pass field
                                                   │ updating: warp moving through it
        Option 1: register_micro() in REG_FINALIZE (in-process)  ──▶ warp
        Option 2: [WAVE 2: PREP_MICRO → TILE×M @2048 (updating) → STITCH_MICRO] ──▶ warp
```

**Routing rule:** when `reg_distributed_tiling=true` AND `skip_micro_registration=false`, the
distributed subworkflow must take Option 1 (or 2) — it must **not** silently drop the micro pass.
Add a verification case: distributed vs classic with micro ON must be pixel-identical.

## 5B. Per-stage memory profile & why only non-rigid/micro are tiled

Verified against VALIS 1.0.0 source. This answers "if rigid/feature aren't tiled, is the memory
gain meaningless?" — **no**, because those stages never touch the full-res image.

| Stage | Runs at | Tiled? | Peak RAM driver |
|---|---|---|---|
| Feature detection (SuperPoint) + matching (SuperGlue) | downsampled `processed_img` @ `max_processed_image_dim_px` (~1024–2048px) | No | ~fixed, tiny — **independent of slide size** (`serial_rigid.py:511`) |
| Initial **rigid** registration | same downsampled image | No (and can't be) | tiny — one global transform from globally-matched features |
| **Non-rigid** | `max_non_rigid_registration_dim_px` (~3000–4096px) | **Yes** when `est_GB>10` | displacement fields × n_slides → the only size-scaling driver |
| **Micro** (§5A) | `micro_reg_size`, `tile_wh=2048` | **Yes**, same rule | same |
| **Warp** to full res | level 0 | streamed | bounded — `pyvips.cache_set_max(0)` streams tiles (`registration.py:35`) |

**Conclusions:**
1. Feature/matching/rigid are **already memory-bounded by downsampling** — tiling them buys ~zero
   RAM and would hurt quality (per-tile rigid = independent local shifts = boundary seams; it
   reinvents non-rigid, worse). So the design deliberately does **not** tile them. Bit-identical
   is preserved precisely because those stages are untouched.
2. The stages whose RAM scales with image size — non-rigid + micro — **are** tiled. That's the
   meaningful lever, and it's the one this design pulls.
3. The everything-per-tile idea is only worthwhile for a *different* goal (full-resolution
   accuracy with no downsampling); for the **memory** goal it adds quality risk for no gain.

## 5C. Low-budget clusters: separate process per step (the real win)

The biggest low-budget killer in the current monolithic `REGISTER` is **not** the tile math — it's
that it holds a **32–64 GB BioFormats JVM heap** *while* doing non-rigid *and* the full-res warp on
one node. The decisive fact for cheap clusters:

> **`REG_TILE` needs no JVM, no BioFormats, no full-res image — just pyvips + OpenCV DeepFlow on
> one tile crop (~1–2 GB).** Hundreds of tile tasks can flood the smallest, cheapest nodes.

So decompose into per-step processes, each sized to its own peak (a low-budget scheduler then
places each where it fits):

```
RIGID_PREP   JVM + downsampled compute (rigid M + emit tiler inputs; NO tile compute)   few medium nodes
  ▼
REG_TILE×N   pyvips + DeepFlow, 1 tile   ~1–2 GB, NO JVM                                 MANY tiny cheap nodes ← win
  ▼
STITCH       assemble field @ non-rigid res   ~medium, no JVM                            few small nodes
  ▼
WARP         JVM + full-res streamed I/O   ~medium                                       few medium nodes
```

**Refinement to §4/§6:** `REG_PREP` should run **rigid only** and emit the tiler inputs, halting
*before* computing any tiles (the "halt before tile compute" variant of the Task-1 spike), so the
heavy compute is fully pushed to the no-JVM `REG_TILE` swarm rather than kept on the fat prep node.
Each process declares a resource label matched to its real peak (`RIGID_PREP`/`WARP` → JVM-sized;
`REG_TILE` → `process_low`, no JVM), so low-budget profiles can cap them independently.

## 6. Two hooking strategies (decision for the plan; recommend Strategy 2)

The per-tile *kernel* externalizes cleanly. The risk is the surrounding **composition** (§5.4).

- **Strategy 1 — externalize `calc()` + reproduce composition.** `REG_STITCH`/`REG_WARP`
  re-implement `serial_non_rigid`'s field composition in our own script. *Pro:* fewer VALIS
  internals re-run. *Con:* we own a faithful port of intricate composition math — a real
  bit-identical hazard.
- **Strategy 2 — inject pre-computed tiles back into VALIS (recommended).** Replace the body of
  `NonRigidTileRegistrar.calc()` so that, instead of computing tiles, it **reads** the per-tile
  `dxdy` fields produced by the distributed `REG_TILE` tasks and only runs `stitch_tiles` +
  VALIS's own downstream composition. `REG_PREP` and the finalize step run inside real VALIS
  calls (registrar state pickled by `REG_PREP`, reloaded by the finalizer), so **all composition
  stays in VALIS** → bit-identical by construction. *Con:* registrar state must be
  serialized/reloaded (VALIS already pickles its registrar to `results_dir`).

Recommendation: **Strategy 2**, because the hard requirement is bit-identical and the
composition is the part most likely to drift if re-implemented.

### How the seam is wired: explicit hook (Option B), not runtime monkeypatch

VALIS exposes no public seam at the tile loop and hardcodes `NonRigidTileRegistrar` internally
(`get_nr_tiling_params`), so `non_rigid_registrar_cls` cannot route to a subclass. Two ways to
intercept `calc()`:

- **A — runtime monkeypatch:** swap `NonRigidTileRegistrar.calc` at import time in our scripts.
  Hidden seam; couples to the method name (safe under the `==1.0.0` pin).
- **B — explicit source hook (CHOSEN).** Add a **~4-line, default-`None` class hook** to
  `NonRigidTileRegistrar`, shipped as a **reviewable `.patch`** applied in the Dockerfile *after*
  `pip install valis-wsi==1.0.0`:

  ```python
  class NonRigidTileRegistrar(object):
      EXTERNAL_TILE_HOOK = None                          # default None ⇒ pristine classic path
      def calc(self, *args, **kwargs):
          if NonRigidTileRegistrar.EXTERNAL_TILE_HOOK is not None:
              return NonRigidTileRegistrar.EXTERNAL_TILE_HOOK(self)   # returns (bk_dxdy, fwd_dxdy)
          # ... original tile-loop body unchanged ...
  ```

  Our scripts set `EXTERNAL_TILE_HOOK` to a **halt hook** (PREP: dump inputs → raise
  `TilesPending`) or a **read hook** (FINALIZE: load tiles → `stitch_tiles`). With the hook
  unset, the classic path is **byte-identical** to upstream 1.0.0 (the guard short-circuits to
  the original body).

Why B: the seam is **visible in source and reviewable as a diff**, rather than swizzled at
import. Cost: the container applies a small patch over the pip-installed VALIS, and we own that
~4-line diff (`containers/valis/calc_hook.patch`). The single module `bin/utils/valis_tiling.py`
owns setting/clearing the hook, so it's the only place that knows about the seam.

> **Process-decomposition note:** under Strategy 2 the §4 `REG_STITCH` + `REG_WARP` boxes
> collapse into a single `REG_FINALIZE` process — the reloaded VALIS registrar runs the
> patched `calc()` (read tiles → `stitch_tiles`), then VALIS's own composition and
> `warp_and_save_slide` in one call. The four-box diagram in §4 reflects the *logical* stages;
> Strategy 2's *physical* layout is `REG_PREP → REG_TILE (fan-out) → REG_FINALIZE`. The plan
> picks the final decomposition.

---

## 7. Data formats & I/O contract between processes

| Artifact | Format | Notes |
|---|---|---|
| `moving.v` / `fixed.v` / `mask.v` | pyvips `.v` or tiled pyramidal OME-TIFF | Must support cheap `extract_area` (tiled, so `REG_TILE` reads only its bbox region, not the whole image — this is what bounds RAM). |
| `bboxes.json` | JSON | `{ slide_id, shape_rc, tile_wh, tile_buffer, n_rows, n_cols, target_stats, tiles: [{idx, bbox_xywh, expanded_bbox_xywh, empty?}] }` |
| `dxdy_<slide>_<i>.v` | float32 pyvips, 2 bands (bk) + 2 bands (fwd) | One per non-empty tile. Lossless. |
| registrar state | VALIS pickle (Strategy 2) | Written by `REG_PREP`, reloaded by finalizer. |
| `*_registered.ome.tiff` | OME-TIFF | Same output contract as classic `REGISTER` (so `channels_manifest.json`, downstream segmentation/quant are unaffected). |

The new path must still emit the same `versions.yml`, `*.size.csv`, and `channels_manifest.json`
as classic `REGISTER` so QC aggregation and the rest of the pipeline don't change.

---

## 8. Parameter design (Nextflow)

### 8.1 The toggle

| Param | Default | Meaning |
|---|---|---|
| `params.reg_distributed_tiling` | `false` | Master switch. `false` → classic `REGISTER` (unchanged). `true` → `REG_PREP → REG_TILE → REG_STITCH → REG_WARP`. |
| `params.reg_dist_sub_threshold` | `'auto'` | `'auto'` = single whole-image tile below VALIS's tiler threshold (§5a). `'force'` = tile regardless (§5b). |
| `params.reg_dist_tiles_per_task` | `1` | Tiles per `REG_TILE` task. `1` = Approach A (max granularity). Higher = batch (Approach B). Does **not** affect output, only scheduling. |

> **Hard precondition (enforced at routing):** distributed mode requires the
> `OpticalFlowWarper` (DeepFlow) non-rigid backend — the pipeline default. It must **refuse to
> run** with a `SimpleElastix`-based backend, which is RNG-dependent per tile and therefore not
> bit-identical under process-level fan-out (§3 precondition 1).

Routing lives in `subworkflows/local/registration.nf`: branch on `params.reg_distributed_tiling`.
Wired through `nextflow_schema.json` + `nextflow.config` like existing `reg_*` params, and
documented in `docs/registration_methods.md`.

### 8.2 The bit-identical VALIS parameter set

Because "classic" = the current `REGISTER` process **as configured**, the distributed path
must consume the **same** VALIS parameters the classic path already passes (memory-mode preset,
reference, crop, feature detector/matcher, micro-registration, interpolation). The *only* added
parameters are the tile-grid knobs, which must equal VALIS 1.0.0's internal tiling defaults so
the grid matches what classic VALIS computes for itself:

| VALIS parameter | Value for bit-identical | Source |
|---|---|---|
| non-rigid registrar | `OpticalFlowWarper` (DeepFlow) | `register.py` default; 1.0.0 default |
| `optical_flow_obj` | `cv2.optflow.createOptFlow_DeepFlow()` | 1.0.0 default |
| `n_grid_pts` | `50` | 1.0.0 default |
| `sigma_ratio` | `0.005` | 1.0.0 default |
| `paint_size` | `5000` | 1.0.0 default |
| `fold_penalty` | `1e-6` | 1.0.0 default |
| `smoothing_method` | `None` | 1.0.0 default |
| `tile_wh` | **`512`** (`DEFAULT_NR_TILE_WH`) | must match classic's internal tiling |
| `tile_buffer` | **`100`** (`NonRigidTileRegistrar` default) | must match classic's internal tiling |
| `get_grid_bboxes(inclusive=...)` | **`True`** | 1.0.0 call site |
| `get_inverse_field(n_inter=...)` | **`10`** | 1.0.0 default |
| `max_non_rigid_registration_dim_px` | = current preset (`high` → `4096`) | inherit from `memory_mode`; must match classic |
| `max_processed_image_dim_px` | = current preset (`high` → `2048`) | inherit from `memory_mode` |
| `reference_img_f`, `align_to_reference`, `crop` | = current (`ref`, `True`, `"reference"`) | inherit from classic call |
| feature detector / matcher | `SuperPointFD` / `SuperGlueMatcher` | inherit from `memory_mode` |
| micro-registration | same as classic (`MicroRigidRegistrar`, `tile_wh=2048`) unless `--skip-micro-registration` | inherit |

> The new tile knobs (`tile_wh=512`, `tile_buffer=100`) are **not** free tuning parameters if
> bit-identical is required — they are pinned to VALIS's internals. They become free only if the
> fidelity bar is relaxed to "equivalent quality."

---

## 9. Verification plan

1. **Golden baseline:** run the classic `REGISTER` (toggle off) on a *big-enough* test slide
   (one that crosses `TILER_THRESH_GB` so classic actually tiles — may require an upscaled or
   synthetic large fixture, since `tests/testdata` is intentionally tiny).
2. **Distributed run:** same inputs, toggle on, `tiles_per_task=1`.
3. **Compare:**
   - Per-slide stitched `bk_dxdy` / `fwd_dxdy`: exact array equality (or `max |Δ| == 0`).
   - Final `*_registered.ome.tiff`: pixel-exact diff per channel.
   - VALIS `error_df` registration metrics: identical.
4. **Granularity invariance:** `tiles_per_task ∈ {1, 4, n_tiles}` must all produce identical
   output (proves batching is purely a scheduling concern).
5. **Sub-threshold parity (`'auto'`):** a small slide with toggle on must match classic exactly
   (single whole-image tile path).
6. **Stub tests:** add `nf-test` stubs for `REG_PREP/REG_TILE/REG_STITCH/REG_WARP` so the
   distributed path runs in the existing fast stub CI loop.
7. **Micro-registration ON (§5A):** with `skip_micro_registration=false`, distributed vs classic
   must be pixel-identical — confirms `register_micro()` is reproduced (Option 1/2), not dropped.

DeepFlow is deterministic, so equality (not just tolerance) is the right bar in the tiling regime.

---

## 10. Risks & open questions

| # | Risk | Mitigation |
|---|---|---|
| R1 | Hand-ported composition diverges from VALIS (§5.4) | Use Strategy 2 (keep composition in VALIS). |
| R2 | `extract_area` on a non-tiled `.v` reads the whole image → no RAM win | `REG_PREP` must write **tiled** images so tile reads are localized. Verify with `vips` header. |
| R3 | Unpinned `valis-wsi` changes the baseline (a rebuild could jump 1.0.0 → 1.2.0, which has a different tile kernel) | Pin `==1.0.0` in Dockerfile (§2). |
| R4 | `target_stats` fallback path not reproduced → off-by-epsilon tiles | Compute & ship `target_stats` from `REG_PREP` (§5.3); covered by verification step 3. |
| R5 | Known VALIS bug: passing `NonRigidTileRegistrar` explicitly leaves `fwd_dxdy=None` (pyvips rejected by Slide setter) — already documented in `register.py:558-566` | Strategy 2 sidesteps it (we don't change the registrar class VALIS picks; we only feed `calc()` precomputed tiles). Keep the existing `fwd_dxdy`-repair safety net. |
| R6 | Per-tile JVM/BioFormats startup cost if `REG_TILE` initializes VALIS heavily | `REG_TILE` should need only pyvips + the optical-flow kernel, **not** the JVM (no BioFormats I/O at tile stage). Confirm `import` cost during planning. |
| R7 | Tiny test data never crosses the tiler threshold | Add a large synthetic fixture for the golden-baseline test (verification step 1). |
| R8 | `tiles_per_task` batching accidentally changes normalization (e.g. shared stats across a batch) | Each tile must still normalize independently; batch only loops `reg_tile(i)` per index. Covered by verification step 4. |
| R9 | A non-DeepFlow backend (`SimpleElastixWarper`) is selected → per-tile RNG sampling makes process fan-out non-reproducible (`non_rigid_registrars.py:685-691,801`) | Hard precondition (§8.1): distributed mode refuses any non-`OpticalFlowWarper` backend. |
| R10 | Re-implemented stitch diverges from VALIS's `mblend` seam blend | Reuse `warp_tools.stitch_tiles` verbatim (Strategy 2); never hand-roll the blend (§3 step 4, §5 precondition 3). |
| R11 | With `skip_micro_registration=false`, the distributed path skips `register_micro()` → not bit-identical to classic-with-micro | §5A: `REG_FINALIZE` runs `register_micro()` in-process (Option 1) or a second wave (Option 2); add a micro-ON verification case (§9.7). |

> **Verification provenance:** §3, §5, and §8.2 were independently re-checked on 2026-06-13 by
> three adversarial reviewers against the 1.0.0 source. Parameter defaults and the regime
> boundary were confirmed exact; the `mblend` stitch behavior, the DeepFlow-only constraint, and
> the per-slide provenance of `target_stats`/`processing_cls`/`mask`/`tile_buffer` were added as
> a result.

---

## 11. Out of scope

- Parallelizing rigid / feature-matching / micro-registration (kept as-is, per slide).
- Changing default behavior for any existing user (toggle defaults off).
- GPU optical flow, alternative non-rigid registrars, or registration-quality improvements.

---

## 12. Next step

On approval, proceed to `writing-plans` to produce the implementation plan
(process scaffolding, the Strategy-2 monkeypatch, schema/config wiring, large test fixture,
and the verification harness above).
