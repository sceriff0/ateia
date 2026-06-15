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

### DECISION (2026-06-15, locked with user): Option 2 — distributed second wave, *separated whole-image*

Two ways were considered:

- **Option 1 — micro in-process inside `REG_FINALIZE`.** A live VALIS registrar in `REG_FINALIZE`
  calls `registrar.register_micro(...)`. Bit-identical, minimal plumbing, **but** micro's big-image
  cost (it is the *higher-resolution, heavier* pass — see below) stays on a single FINALIZE node,
  re-introducing exactly the JVM-heap + serial bottleneck this whole design exists to remove. **Rejected.**
- **Option 2 — distribute micro as a second wave. CHOSEN.** Micro mirrors wave 1's *process
  separation* (§6.7), which is where the real RAM win comes from — not tiling. Micro runs in its own
  **JVM-free, per-slide-parallel** process; it only fans out to tiles (`tile_wh=2048`) in the rare
  case its `est_GB` crosses the 10 GB threshold (same fork as wave 1's `reg_dist_force_tiling`).

**Why micro is the heavier pass (and why this matters):** `micro_reg_size = floor(min_max_full_res_dim
× micro_reg_fraction)` with `micro_reg_fraction=0.125` (`bin/register.py:746-747`), while the main pass
is fixed at `DEFAULT_MAX_NON_RIGID_REG_SIZE = 3000`px. For a ≥24k-px WSI, `micro_reg_size ≥ 3000` — i.e.
micro runs at *equal or higher* resolution than the main pass (its own log: "may take 30-120 minutes").
Running it in-process in FINALIZE is the bottleneck; running it as a separated per-slide process is not.

**The chain (gated on `!skip_micro_registration`), reusing wave-1 machinery:**

```
REG_PREP → REG_NONRIGID (wave-1 bk.v + fwd.v)
   │
   ├─ skip_micro=true ─────────────────────────────────────→ REG_FINALIZE (compose+warp)
   │
   └─ skip_micro=false → REG_MICRO_PREP ─→ REG_NONRIGID ─────→ REG_FINALIZE
        (JVM; inject wave-1 field via      (= reuse, JVM-free,   (additive compose:
         stored_dxdy, prep updating         per-slide; tile@2048   updated = scale(wave1)
         _non_rigid=True @micro_reg_size,    only if micro crosses  + pad(residual), for
         no-op warper → capture micro        the 10 GB threshold)   BOTH bk and fwd; then
         2-D inputs + full_out_shape_rc                             pad → warp_slide → save)
         + mask_bbox; halt before DeepFlow)
```

- **`REG_MICRO_PREP`** (new): mirror `reg_prep.py` (no-op warper to capture 2-D inputs cheaply, JVM,
  bounded — halts before DeepFlow), but first **inject the real wave-1 `bk`/`fwd` field** onto the
  slide (`stored_dxdy`) so the `updating_non_rigid=True` prep warps the moving image through `M` +
  wave-1 field before capturing the micro 2-D residual inputs. Dump micro `tiler_inputs/` + a micro
  `warp_state` carrying `full_out_shape_rc` and `mask_bbox` (for the pad at `registration.py:4314`).
- **micro non-rigid:** reuse `reg_nonrigid.py` unchanged (separated whole-image, default) — extended to
  also emit `fwd.v` — or `reg_tile.py` at `tile_wh=2048` only if `est_GB > threshold`.
- **`REG_FINALIZE`:** extend with `--micro-residual`/`--micro-warp-state`; apply the **spike-proven**
  (`spike_micro_option2.py`, `max|Δ|=0`) additive compose for both `bk` and `fwd`, then the existing
  pad → `slide_tools.warp_slide` → OME save. No new heavy node.

**Routing rule:** when `reg_distributed_tiling=true` AND `skip_micro_registration=false`, the distributed
subworkflow runs the micro wave above — it must **not** silently drop the micro pass. Verification (§9.7):
distributed-with-micro vs classic-with-micro must be pixel-identical (`max|Δ|=0`), proven locally in
`mirage-valis:1.0.0` on the large fixture.

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

## 6.1 Spike result (Task 1, run 2026-06-14 in `mirage-valis:1.0.0`)

Run via `bin/spikes/spike_externalize_tiles.py` on the P001 test slides. The spike drives
`NonRigidTileRegistrar` directly with a forced **3×3 (9-tile)** grid (small `tile_wh`) so the
`mblend` stitch seam is genuinely exercised, recomputes each tile in a **separate OS process**
(subprocess), and compares to an unmodified-VALIS baseline.

### ✅ Make-or-break PROVEN — externalized tiles + VALIS `stitch_tiles` == in-process `calc()`, exactly
- Per-tile, fresh-process `reg_tile` vs the threaded loop's tile: `max|Δ| = 0` (9/9 tiles).
- Read-mode `stitch_tiles(recomputed_tiles)` vs baseline **`bk_dxdy` and `fwd_dxdy`**: `max|Δ| = 0`.
- ⇒ Strategy 2's tile externalization + `stitch_tiles` reuse is **bit-identical by construction**,
  seam included. **Phase 1 is unblocked.** (Used `processing_cls=None`, the deterministic
  grayscale path, to isolate the mechanism from Blocker 3 below.)

### ❌ BLOCKER 1 — pyvips images are unpicklable ⇒ NO cross-process registrar handoff
- A bare `pyvips.Image` cannot be pickled: `TypeError: cannot pickle '_cffi_backend._CDataBase'`
  (the libvips handle is a cffi pointer). Confirmed by 2-line repro.
- VALIS's **own** end-of-register registrar pickle fails to reload in a fresh process:
  `TypeError: Class type incorrect`. `registration.load_registrar` is just `pickle.load(...)`
  (registration.py:142), so it would fail identically — the registrar embeds pyvips images.
- **Consequence (supersedes part of §6):** the §5C plan of "`REG_PREP` pickles the `Valis`
  registrar → `REG_FINALIZE` reloads it" is **NOT viable** in this image. The
  `REG_PREP → REG_TILE → REG_FINALIZE` boundary **must exchange plain serializable data**
  (`.npy` displacement fields, JSON/`.npy` per-slide metadata, `.v` tile images on disk — exactly
  what the spike's working dump/read path uses), never a pickled `Valis`. `REG_FINALIZE` must
  reconstruct composition + warp from that plain state — per-slide rigid `M`, image shapes,
  `bg_color`, crop bbox, reference `dxdy`, `target_stats`, processing config — either by rebuilding
  a minimal `Slide` and calling `warp_and_save_slide`, or by re-implementing the
  `serial_non_rigid` composition (the Strategy-1 option, R1). The **tile kernel + `stitch_tiles`
  reuse still stand** (proven above); only the "keep the live registrar across processes" leg dies.
  *Phase-2 follow-up:* identify the exact unpicklable slide attributes and whether they can be
  dropped + re-derived from disk, or whether a newer pyvips fixes Image pickling.

### ❌ BLOCKER 2 — `register()` swallows all exceptions ⇒ cannot raise-to-halt
- `TilesPending` raised inside `calc()` is caught by `Valis.register()`'s broad `except Exception`
  (registration.py ~4149), which logs it, `kill_jvm()`s, and **returns `None`** — it never
  propagates to the caller. (Rigid state still persists on the slide objects.)
- **Consequence:** `install_halt_hook` cannot rely on an exception reaching `REG_PREP`. Halt must
  be signaled by **side effect**: the hook dumps tiler inputs to disk, and `REG_PREP` detects dump
  completion (and tolerates `register()` returning `None`). The `EXTERNAL_TILE_HOOK` patch should
  dump-then-signal via the filesystem, not via a raised exception.

### ❌ BLOCKER 3 — `ChannelGetter` crashes in the tile path ⇒ classic tiling is broken for fluorescence
- `ChannelGetter.process_image` (preprocessing.py:113-114) calls
  `get_slide_reader(self.src_f)(self.src_f)` **unconditionally**, *before* the `if self.image is
  None` check. `NonRigidTileRegistrar.process_tile` hardcodes `src_f=None`, so any `ChannelGetter`
  (fluorescence/multichannel) tile raises `FileNotFoundError: 'None'`. Confirmed both in a real
  `Valis.register()` run and via a standalone 2-line repro with a valid 2-D tile array.
  Brightfield (`ColorfulStandardizer`) is unaffected (it uses only `self.image`).
- **Consequence:** classic VALIS auto-tiling (`est_GB > 10`) **itself crashes on fluorescence** —
  so there is *no working classic fluorescence-tiling baseline* to be "bit-identical" to. The
  distributed path must feed the tiler **pre-reduced single-channel (2-D) images with
  `processing_cls=None`** (the deterministic fallback Part A/B validated), or ship a guarded
  `ChannelGetter`. §9's fluorescence verification must compare distributed output against the
  **whole-image classic** result, not the broken tiled one. (Brightfield can still verify against
  classic tiling directly.)

### Net decision
1. **Proceed to Phase 1** (Tasks 2–3: `tile_grid`, `tile_io`) — pure kernels, unaffected by the blockers.
2. **Before Phase 2**, revise the PREP/FINALIZE contract: plain-data handoff (drop the
   pickled-registrar assumption, Blocker 1), filesystem-signalled halt (Blocker 2), and
   `processing_cls=None` + pre-reduced channels for the tiler (Blocker 3).
3. The spike (`bin/spikes/spike_externalize_tiles.py`) is **kept** (not deleted as originally
   planned) — it is the reproduction harness for Blockers 1–3 and the bit-identical regression check.

---

## 6.2 Revised architecture (decision 2026-06-14, after §6.1 spike) — Option A: plain-data handoff

Chosen with the user (goal: cheap RAM). The per-step decomposition (the RAM win) is preserved; the
only change vs §4/§6 is that the PREP→FINALIZE handoff is **plain serializable data**, not a pickled
`Valis` (Blocker 1). Verified feasible against the 1.0.0 `Slide.warp_slide` source (it already
supports a disk-backed `dxdy`: `pyvips.Image.new_from_file(bk_dxdy_f)`, registration.py:626).

**Why Option A for low RAM:** the RAM win comes from the decomposition itself (no-JVM `REG_TILE`
swarm; PREP/FINALIZE separated so no single node holds JVM-heap *and* non-rigid *and* full-res warp).
Option A additionally **reuses VALIS's own `warp_and_save_slide`, which streams the full-res warp via
`pyvips.cache_set_max(0)`** (registration.py:35) — bounded RAM regardless of slide size. Re-rolling
the warp ourselves (Strategy 1) would risk loading full-res into RAM; Option A avoids that.

### Physical stages (Strategy 2 kernel, Option-A handoff)

```
REG_PREP  (JVM)  rigid only; per slide PROCESS images to the tiler's 2-D form (src_f available here,
                 so ChannelGetter works), dump:
                   tiler inputs: moving.v (processed, 2-D), fixed.v, mask.v, expanded_bboxes(.npy),
                                 target_stats(.npy), manifest.json   [processing_cls = None downstream]
                   warp state:   per-slide M, processed/reg/aligned shapes, bg_color, crop, src_f,
                                 slide_dimensions_wh, reference shapes + crop mask   (.npy / .json)
                 halt is signalled by SIDE EFFECT (dump dir populated); register() swallowing the
                 hook's exception and returning None is expected (Blocker 2). NO registrar pickle.
  ▼ fan-out 1 task/tile
REG_TILE  (no JVM, ~1-2 GB)  VALIS reg_tile on the pre-processed 2-D tile with processing_cls=None
                 (the deterministic path Part A/B proved bit-identical). Emits bk_<i>.v / fwd_<i>.v.
  ▼ fan-in (groupTuple by slide)
REG_FINALIZE (JVM)  warp_tools.stitch_tiles(tiles) -> full bk/fwd; rebuild a MINIMAL VALIS Slide from
                 the dumped plain warp state + stitched dxdy; call slide.warp_and_save_slide(...)
                 (+ register_micro in-process if micro on, §5A). All warp/compose math stays in VALIS.
```

### Consequences for the plan
- **Task 4** (`EXTERNAL_TILE_HOOK` patch): keep, but only the **dump/halt** hook is used (by PREP).
  FINALIZE does NOT re-enter `calc()`; it stitches directly. The hook signals halt via the
  filesystem, not a raised exception that PREP catches.
- **Task 5** (`reg_prep.py`): dump plain warp state + processed 2-D tiler inputs; do **not** pickle
  the registrar. Process fluorescence to its DAPI channel here.
- **Task 7** (`reg_finalize.py`): stitch + rebuild minimal Slide + `warp_and_save_slide`; no
  registrar reload.
- **Open de-risk before Task 5/7 (Task 4.5, the next make-or-break):** FINALIZE must reproduce the
  post-tiler composition that `serial_non_rigid.calc_deformation` does on the stitched field
  (serial_non_rigid.py:460-503), then warp — and prove it **pixel-identical** to a full classic run.
  The composition is a fixed sequence of **VALIS functions** (not hand-rolled math), applied to the
  stitched `moving_bk_dxdy`:
  1. `warp_tools.remove_invasive_displacements(moving_bk_dxdy, M, src_shape_rc=unwarped_shape, out_shape_rc=og_reg_shape_rc)` when `from_rigid_reg`;
  2. `mask_dxdy(·, reg_mask)` when a non-rigid mask exists;
  3. `bk_dxdy_from_ref = bk_dxdy + moving_bk_dxdy` — **combine with the field of the image this slide
     was aligned *to*** (the reference's field for slides adjacent to the reference; an *accumulated*
     field for non-adjacent slides in `align_to_reference` serial order — this cross-slide
     accumulation is the one real complication and must be dumped/threaded per slide);
  4. `remove_invasive_displacements` again on the masked copy → `self.bk_dxdy`;
  5. `self.fwd_dxdy = warp_tools.get_inverse_field(self.bk_dxdy)` (default `n_inter=10`).
  PREP halts *before* non-rigid, so it does NOT have the reference/accumulated `bk_dxdy` from step 3;
  the spike must determine how FINALIZE obtains it (e.g. process the reference slide's tiles first and
  publish its composed field as an input to the moving slides' FINALIZE, honoring serial order). For
  the common 1-ref+1-mov case the reference field is identity, so step 3 reduces to
  `bk_dxdy_from_ref = moving_bk_dxdy` — start the spike there, then generalize.

---

## 6.3 Task 4.5 spike result (run 2026-06-14 in `mirage-valis:1.0.0`) — Option-A FINALIZE PROVEN

Run via `bin/spikes/spike_finalize_option_a.py all`, 1-ref + 1-mov P001 fluorescence pair,
production-matching config (`align_to_reference=True`, `crop="reference"`, `create_masks=True`,
`OpticalFlowWarper`, no micro). Baseline is **whole-image classic** `Valis.register()` (the tiler is
NOT forced — Blocker 3 crashes classic fluorescence tiling; the stitched-field identity is already
proven by §6.1). The rebuild runs in a **fresh process from plain data only** (no pickled `Valis`).

### ✅ All four legs bit-identical (`max|Δ| = 0`)
- **P1 compose** — port of `calc_deformation` 460-503 on the captured `moving_bk_dxdy` reproduces the
  classic `self.bk_dxdy` **exactly**.
- **P2 warp** — `slide_tools.warp_slide(...)` is a **pure plain-data function** (`src_f`, `M`,
  `transformation_src/dst_shape_rc`, `aligned_slide_shape_rc`, `dxdy`, `bbox_xywh`, `bg_color`,
  `level`, `series`); fed the classic field it reproduces the classic warped slide **exactly**. ⇒
  **FINALIZE needs no live/rebuilt `Slide` object for the pixels** — only this fn + the dumped scalars.
- **P3 handoff** — `extract_area(bbox)` → lossless `tiffsave` (lzw, float) → `new_from_file` preserves
  the field **exactly** (the disk-backed `dxdy` leg the tiler path uses, registration.py:3717-3720/626).
- **CHAIN** — end-to-end `moving_bk_dxdy → compose → pad_displacement → warp_slide` == classic warped
  slide, **exactly**.

### 🔑 Key correction to §6.2 (observed, not assumed) — `from_rigid_reg` is FALSE in production
`non_rigid_register` sets `nr_on_scaled_img = (max_processed_image_dim_px != max_non_rigid_registration_dim_px)
or (mask crops the image)` (registration.py:3625-3626). This is **True in every preset** (test 256≠1024;
mid 1024≠4096; high 2048≠4096) **and** whenever `create_masks=True` crops — so the non-rigid runs on a
**dict** of pre-scaled images, and `SerialNonRigidRegistrar.__init__` sets `from_rigid_reg=False`
(serial_non_rigid.py:642-647). Consequence: the `remove_invasive_displacements` calls (compose steps 1 & 4,
lines 460-465 / 482-487) are **NOT executed** in production. The real FINALIZE compose is just:
**(optional `mask_dxdy`) → add reference field → (optional `mask_dxdy`)**, then `get_inverse_field`.
This is simpler than §6.2's worst case; Task 7 must gate the remove-invasive steps on `from_rigid_reg`.

### Dump contract confirmed (what PREP must emit for FINALIZE), per slide
- `moving_bk_dxdy` (from REG_TILE stitch) — native type matters: it's a **numpy `[dx,dy]`** here (the
  `is_array` branch of `NonRigidTileRegistrar.register`), so FINALIZE must preserve native vips/numpy
  type, not silently coerce (the compose add branches on type, 467 vs 473).
- `from_rigid_reg` flag (+ `M`, `unwarped_shape`, `og_reg_shape_rc` only if True — unused in production).
- reference/incoming field (identity when `align_to_reference=True`) + serial order (only needed for the
  accumulation case, which `align_to_reference=True` avoids entirely).
- internal pad `(out_shape_rc, bbox_xywh)` mapping the masked compose field → full reg-resolution field
  (line 3671); `_full_displacement_shape_rc`, `_non_rigid_bbox` for the disk store/pad.
- warp contract: `src_f`, `M`, `transformation_src_shape_rc` (= `processed_img_shape_rc`),
  `transformation_dst_shape_rc` (= `reg_img_shape_rc`), `aligned_slide_shape_rc`, resolved crop
  `bbox_xywh`, `bg_color`, `series`, `level`.

### Scope / honest limitations (follow-ups, not blockers)
1. **1-ref+1-mov only.** `align_to_reference=True` (production) makes every moving slide the same
   identity-incoming case, so generalization to N moving slides is mechanical. The cross-slide
   **accumulation** path (spec step 3) only arises under `align_to_reference=False`, which the pipeline
   does not use — out of scope unless that changes.
2. **Mask branches in compose not exercised:** the `mask` arg to `calc_deformation` was `None` in this
   config (the mask crops the *image* pre-registration, but isn't threaded into the compose), so the
   `mask_dxdy` branches are dead in this path. Re-verify if a future config passes a non-rigid mask.
3. **No micro-registration** (§5A) — separate concern; FINALIZE-with-micro is still Option 1 (in-process).
4. Baseline is whole-image (not tiled) by necessity (Blocker 3); the tile→stitch identity is §6.1's job.

**Net:** Option-A plain-data FINALIZE is proven feasible and bit-identical for the production path.
`bin/spikes/spike_finalize_option_a.py` is **kept** as the FINALIZE regression harness. Tasks 5 & 7
can be finalized: PREP dumps the contract above; FINALIZE = compose (gated on `from_rigid_reg`) →
`pad_displacement` → `slide_tools.warp_slide`, no `Slide`/`Valis` rebuild for the pixels.

---

## 6.4 PREP halt finding (probe 2026-06-14) — the tiler dumps UNPROCESSED multichannel images ⚠️

Probed by forcing the tiler (`registration.TILER_THRESH_GB = 0`) + `install_halt_hook` on the P001
fluorescence pair, production config. **Confirmed working:**
- The halt fires; `Valis.register()` **swallows** `TilesPending` (Blocker 2) and returns; PREP detects
  the halt by the populated dump dir. ✅
- After the halt, the rigid + non-rigid-prep state needed for FINALIZE's warp contract **survives** on
  the `Valis`: `slide_dict` (per-slide rigid `M`, `reg_img_shape_rc`, `bg_color`), `_non_rigid_bbox`,
  `_full_displacement_shape_rc`. ✅ (No registrar pickle needed — read these off `reg` in PREP.)

**Blocker found (revises Task 5):** the dumped `moving.v` is **3-band (multichannel), NOT the 2-D DAPI
channel** — e.g. `106×105 bands=3`. VALIS's tiler-prep takes the *unprocessed* warp branch
(`prep_images_for_large_non_rigid_registration`, registration.py:3546-3555) and **defers channel
processing to per-tile** (the processed-to-2-D branch at 3525-3544 runs only when `use_tiler` is
False). This is the root of Blocker 3: the tiler is *designed* to `ChannelGetter`-process each tile
(crashing on `src_f=None`). Consequence: feeding REG_TILE the dumped 3-band image with
`processing_cls=None` would compute the field on raw multichannel data — **NOT bit-identical** to
whole-image classic, which registers on the processed 2-D DAPI image.

**⇒ Task 5 needs its own make-or-break spike** (the next one). PREP must hand the tiler the **same 2-D
processed image whole-image non-rigid uses**. Two candidate approaches to spike:
  - **(A) Intercept after processing:** drive prep so the processed-2-D branch runs (or call the
    `ChannelGetter` pipeline 3525-3544 directly with `src_f` live), capture those 2-D images, then
    drive `NonRigidTileRegistrar` on them with `processing_cls=None` + the halt hook (mirrors how the
    §6.1 / §6.3 spikes drove the tiler directly on 2-D inputs). Most likely bit-identical.
  - **(B) Guarded `ChannelGetter`:** ship a patch so `process_image` tolerates `src_f=None` and uses
    `self.image`; lets the tiler process per-tile. Risk: per-tile DAPI extraction may differ from
    whole-image at tile seams — must be proven bit-identical, not assumed.
  The spike's gate: **distributed (2-D tiler) field == whole-image classic field, exactly**, for
  fluorescence. Until then, REG_PREP/REG_FINALIZE production scripts are NOT safe to finalize.

(Note: at the test image size with default `tile_wh`, the forced grid is 1×1 — functionally fine, but
use a small `tile_wh` to exercise multi-tile stitch when validating, as §6.1 did.)

---

## 6.5 Regime-boundary finding (probe 2026-06-14) — tiled DeepFlow ≠ whole-image DeepFlow ‼️

Measured directly on the **same 2-D grayscale input** (P001 mov1→ref), `OpticalFlowWarper` (whole)
vs `NonRigidTileRegistrar` (tiled), comparing the returned `bk_dxdy`:

| Comparison | equal | max\|Δ\| (px) | mean\|Δ\| |
|---|---|---|---|
| whole-image vs multi-tile (3×3) | ❌ | **8.38** | 1.80 |
| whole-image vs single-tile (1×1) | ❌ | **8.63** | 1.63 |
| multi-tile vs single-tile | ❌ | 1.14 | 0.47 |

**Tiled and whole-image non-rigid are different algorithms.** Even a single 1×1 tile diverges from
whole-image (the tiler adds per-tile processing/normalization + `tile_buffer` bbox expansion). So:

1. **The Task 5/§6.4 gate "distributed field == whole-image classic field" is UNACHIEVABLE.** Drop it.
2. **The field baseline for the distributed path is the IN-PROCESS TILER on identical 2-D inputs**, not
   whole-image. This is exactly what §6.1 (Task 1) and `reg_tile.py`'s check already use, and it's
   `max|Δ|=0` there. "Bit-identical to *classic*" holds literally **only for large brightfield**, where
   classic itself engages `NonRigidTileRegistrar` (`est_GB > 10`).
3. **For fluorescence there is NO bit-identical classic baseline:** whole-image is a different algorithm
   (~8px off) and classic-*tiled* crashes (Blocker 3). The distributed-tiled path is the *only* way to
   tile fluorescence — it reproduces the tiled algorithm exactly, but cannot be "bit-identical to
   classic" because working classic fluorescence tiling does not exist. Validate fluorescence against
   **in-process tiler on the same 2-D images** (the achievable, meaningful baseline).
4. **§8.1 `reg_dist_sub_threshold='auto'` (single whole-image tile below threshold) is NOT equal to
   classic whole-image** either — a 1-tile tiler ≠ whole-image. Below the genuine tiling threshold the
   distributed path should fall back to **classic whole-image `REGISTER`**, not a 1-tile tiler, if
   matching classic matters. (Re-confirm: does Task 4.5's whole-image warp baseline still apply? Yes —
   Task 4.5 proved *warp-given-a-field*; it is agnostic to how the field was produced.)

**Corrected end-to-end story (supersedes the "compare vs whole-image" lines in §6.1 net-decision &
§9/§10):**
```
PREP   : produce the 2-D processed images classic non-rigid would use (ChannelGetter w/ src_f live,
         norm, rescale, mask — registration.py:3525-3544); dump them + grid + warp-state; halt.
REG_TILE: tile those 2-D images -> field  (== in-process NonRigidTileRegistrar, proven §6.1)
FINALIZE: compose (gated on from_rigid_reg) -> pad -> slide_tools.warp_slide  (proven §6.3)
VALIDATE: distributed end-to-end == in-process tiler on the SAME 2-D images + same compose/warp.
          Additionally, for LARGE BRIGHTFIELD, == classic Valis.register() directly (true regime).
```

**⇒ revised Task 5 spike gate:** PREP's dumped 2-D images, run through the in-process tiler, must equal
the field from classic's own processed-2-D pipeline — i.e. PREP reproduces classic's *processing*
exactly. The *tiling* equivalence is already §6.1. Open question worth a quick check: does a guarded
`ChannelGetter` (Blocker 3 fix-B) let classic auto-tiling run on fluorescence and produce the SAME
field as feeding pre-processed 2-D (fix-A)? If yes, fix-B is simpler for PREP. **This is a candidate
decision point for the user** (fix-A processing-in-PREP vs fix-B guarded-ChannelGetter; and the
below-threshold fallback policy).

---

## 6.6 RESOLVED architecture (decided with user 2026-06-14) — fix-A, with the exact PREP recipe

User priorities: **tiling + low RAM + as close to base VALIS 1.0.0 as possible**; below-threshold →
**fall back to classic `REGISTER`**. Source analysis settles the rest:

- **`processing_cls=None` on a multichannel tile does `rgb2gray` (a blend of all channels), NOT DAPI**
  (non_rigid_registrars.py:1326-1338). So the tiler must be fed the **already-processed 2-D DAPI**
  image; then reg_tile's `ndim==2` branch uses it as-is + `norm_tiles`.
- A guarded `ChannelGetter` (fix-B) still can't map "dapi"→index per-tile without `src_f`
  (preprocessing.py:122 needs a reader). Mapping requires either `src_f` (None per-tile) or a baked
  index — i.e. extra plumbing. fix-A avoids it entirely.
- **Decision = fix-A:** PREP produces base VALIS's own processed 2-D images and feeds the tiler
  `processing_cls=None`. This reuses the proven pieces exactly: the 2-D images are VALIS's (captured
  live), the tile kernel is VALIS's (§6.1, `max|Δ|=0`), compose+warp is VALIS's (§6.3, `max|Δ|=0`).
  So the distributed result is **bit-identical, by construction, to running VALIS's tiler in-process
  on VALIS's own processed 2-D image.** (Caveat recorded: for *brightfield* where base VALIS tiles
  per-tile with `ColorfulStandardizer`, fix-A — process-whole-then-tile — would differ slightly from
  base-VALIS-tiled; exact brightfield-tiled parity is a follow-up via per-tile `ColorfulStandardizer`,
  which needs no guard. The fluorescence WSI case — where base VALIS tiling is *broken*, Blocker 3 —
  is the primary use case and is served exactly.)

### PREP recipe (fix-A) — how to get the processed 2-D images WITHOUT running DeepFlow (low RAM)
1. JVM up; `registration.TILER_THRESH_GB = <high>` so `prep_images_for_large_non_rigid_registration`
   takes its **processed branch** (3513 `if not use_tiler:` → 3525-3544: `ChannelGetter` w/ `src_f`
   LIVE → `rescale_intensity` → `equalize_adapthist` → uint8 → mask → norm). These run at the
   non-rigid resolution (downsampled) — cheap.
2. **Hook `OpticalFlowWarper.register`** to capture its 2-D inputs (`moving_img`, `fixed_img`, `mask`)
   per slide, then **raise `TilesPending`** — halting BEFORE the expensive whole-image DeepFlow. This
   is the §6.4 halt, but on the whole-image warper (use_tiler=False) instead of the tiler.
   `Valis.register()` swallows it (Blocker 2); rigid + prep-to-2-D state persists.
3. Dump per moving slide: `moving.v`/`fixed.v`/`mask.v` (the captured 2-D), `target_stats.npy`,
   the grid via `bin/utils/tile_grid.py` (matches VALIS's grid exactly, Task 2) → `expanded_bboxes.npy`
   + `manifest.json` (the `valis_tiling._dump_inputs` contract; `non_rigid_registrar_cls` =
   OpticalFlowWarper, `processing_cls=None`), and the **warp-state** for §6.3 FINALIZE:
   `from_rigid_reg` (=False in prod), per-slide `M`, `processed_img_shape_rc`, `reg_img_shape_rc`,
   `aligned_slide_shape_rc`, resolved crop `bbox_xywh` (compute via the slide's crop helpers post-rigid),
   `bg_color`, `series`, `src_f`, the reference/incoming field (identity for align_to_reference=True),
   `_non_rigid_bbox`, `_full_displacement_shape_rc`, and the internal pad `(out_shape, bbox)`.
4. `kill_jvm()`. No registrar pickle (Blocker 1).

### Make-or-break gate for the PREP spike (achievable, replaces §6.4/§6.5 gates)
`REG_TILE(PREP's 2-D images) → stitch → §6.3 compose → warp` **==** in-process `NonRigidTileRegistrar`
on the *same* captured 2-D images → compose → warp. (Each leg already `max|Δ|=0`; the spike confirms
PREP wires them together correctly + that the warp-state dump is complete.) Below-threshold routing
sends small inputs to classic `REGISTER` (no tiler), so no parity claim is needed there.

### Remaining implementation (all design-resolved — no unknowns)
- **Task 5** `bin/reg_prep.py` — the recipe above. **Task 7** `bin/reg_finalize.py` — load dumped
  field tiles → `warp_tools.stitch_tiles` → §6.3 compose (gate on `from_rigid_reg`) →
  `pad_displacement` → `slide_tools.warp_slide` → save OME-TIFF (+ `versions.yml`, `*.size.csv`,
  `channels_manifest.json` per §7) (+ `register_micro` in-process if §5A on).
- **Tasks 8-9** NF processes `REG_PREP`/`REG_TILE`/`REG_FINALIZE` + `subworkflows/local/registration.nf`
  routing on `params.reg_distributed_tiling` (+ below-threshold → classic `REGISTER`), params, schema.
- **Task 10** verify: distributed == in-process tiler on same 2-D (fluorescence) + == classic for
  large brightfield; large fixture.

---

## 6.7 STRATEGIC finding (2026-06-14) — VALIS's auto-tiler rarely fires; non-rigid RAM is already bounded ‼️

While implementing the `est_GB` router (the §6.5/§8.1 fallback), the exact VALIS formula revealed a
project-level truth that reframes the value of tiling:

- VALIS computes `est_GB` **at the non-rigid resolution** (`full_out_shape = get_aligned_slide_shape(s)`
  with `s ≈ max_non_rigid_dim / processed_dim`, registration.py:3427-3430), NOT full-res. So the
  non-rigid pass runs at ~`max_non_rigid_dim` (≈4096) **regardless of slide size**.
- `est_GB = n_slides · (img + displacement + processed)` with VALIS's `calc_memory_size_gb`
  (`= nch·H·W·8/bitdepth / 2³⁰`). At a 4096²-ish non-rigid shape with a few slides this is **~0.1 GB**.
  A 40000×40000 WSI → still ~0.1 GB at the non-rigid stage.
- Threshold math: `est_GB > 10` needs **`max_non_rigid_dim ≳ 34,000 px` OR `≳ 200 slides`**. Micro is
  even smaller (`micro_reg_size = min_max·0.125`), so it tiles even more rarely.

**Consequences:**
1. **For typical configs (`max_non_rigid_dim` 1024–4096, few slides) VALIS NEVER tiles** → classic
   non-rigid RAM is already bounded by downsampling. So in **`'auto'` mode the distributed path almost
   never activates** — which is *correct* (it routes to classic = identical), but means tiling delivers
   no RAM win in that regime.
2. The distributed tiling win is real only when you **`'force'`** it (or set `max_non_rigid_dim` very
   high / have hundreds of slides) — and `'force'` differs from classic-whole-image (§6.5).
3. **The user's RAM spikes are likely NOT the non-rigid tile math.** The genuine size-scaling RAM
   drivers in this pipeline are elsewhere:
   - the **BioFormats JVM heap** (32–64 GB) held while reading full-res slides (the §5C lever — solved
     by *process separation*, i.e. no-JVM REG_TILE nodes, NOT by tiling per se);
   - full-res **slide reading / conversion / warp I/O** (warp is streamed via `cache_set_max(0)`, but
     conversion/preprocessing may not be);
   - `memory_mode='high'` pushing `max_processed/non_rigid_dim` up.
   → **Before investing further (e.g. distributing micro), confirm where RAM actually spikes** (profile
   a real run). If it's the JVM/full-res I/O, the §5C *decomposition* (separate JVM-sized PREP/WARP from
   no-JVM TILE) is the lever — and that only helps once tiling engages, i.e. under `'force'` / high-res.

**This does not invalidate the work** — the distributed path is correct and bit-identical to VALIS's
tiler, and the `'auto'` router guarantees identity. It *does* mean the headline benefit is narrower than
"low RAM for any large slide": it's "low RAM when the non-rigid/micro pass itself is large (high-res or
many-slide), via no-JVM tile fan-out." Set expectations accordingly.

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
