# Low-Memory, Parallel, VALIS-Faithful Registration — Design

- **Date:** 2026-07-22
- **Branch:** `feature/reg-lowmem-parallel`
- **Status:** design approved, not yet implemented
- **Supersedes nothing.** Extends the distributed registration path introduced by
  `docs/superpowers/specs/2026-06-13-valis-tiled-parallel-registration-design.md`.

## 1. Goal

Run mirage registration on full-resolution WSIs on a low-resource machine (a laptop or
small workstation, RAM budget expressed as a parameter rather than assumed), while
diverging from classic VALIS as little as physically possible. Must work under
`mode=add_cycle`. Must ship a built-in facility to run classic and new registration over
the same slides and report their difference.

`reg_qc=2` (GeoJSON segmentation-overlap QC) is explicitly **out of scope** for the new
path — it requires a registrar pickle the decomposed path does not produce.

## 2. The core finding

The RAM wall is not the registration algorithm. It is the slide **reader**.

- `valis_lib/slide_tools.py:284-296` — `warp_slide()` is `reader.slide2vips()` **plus**
  `warp_tools.warp_img()`. The warp itself is pure, lazy pyvips
  (`pyvips.Interpolate.new`, `mapim` — `valis_lib/warp_tools.py:248`). Only the read is eager.
- `valis_lib/slide_io.py:909-913` — `BioFormatsSlideReader.slide2vips()` decodes every tile
  through the JVM and stitches them, materializing the whole decompressed slide.
- `valis_lib/slide_io.py:2418-2424` — for OME-TIFF, VALIS selects the JVM-free
  `VipsSlideReader` **only when `is_rgb`**. Multichannel fluorescence therefore always falls
  through to BioFormats. VALIS's own comment states the reason is speed
  ("Converting a multichannel pyvips.Image is very slow, but is fast for RGB"), not correctness.
- `bin/preprocess.py:391-399` — every image entering mirage registration is written by
  tifffile as a **tiled (2048×2048), BigTIFF, zlib** OME-TIFF. It is randomly addressable.
- `valis_lib/registration.py:3982, 4099` — `Valis.register(reader_cls=...)` is a **supported
  extension point**.

Therefore the low-RAM path is a reader, not a reimplementation. Supplying a lazy pyvips
reader makes the existing, unmodified `warp_img` pipeline stream in `O(tile)` RAM.

## 3. Guarantee ladder

Ordered strongest first. Nothing in this design modifies a VALIS algorithm.

| Change | Guarantee | Basis |
|---|---|---|
| Lazy pyvips reader | bit-identical | same TIFF, same `warp_img`; rests on assumption **A1** below |
| Output-tile warp fan-out | bit-identical **by construction** | pyvips is demand-driven: `warp(...).crop(x,y,w,h)` computes each output pixel with the same code as the whole-image warp, pulling exactly the source region needed including bicubic halo |
| Everything downstream | unchanged | already covered by `tests/integration/verify_micro_bitidentical.py` |

### Assumptions to validate before implementation

- **A1 — pyvips and BioFormats decode mirage's preprocessed TIFF to identical pixels.**
  True for uncompressed / LZW / deflate-zlib (mirage uses `compression='zlib'`), can differ
  for some JPEG variants. Probe first.
- **A2 — `reader_cls=` keeps the JVM out of `Valis.register()` for our inputs.**
  `slide_io.get_slide_reader()` calls `init_jvm()` unconditionally at
  `valis_lib/slide_io.py:2338`, so any residual call site re-summons the JVM. The two known
  remaining call sites (`valis_lib/registration.py:983, 995`) are inside
  `Slide.warp_and_save_slide`, which the decomposed path does **not** use — but this must be
  confirmed empirically, not by inspection alone.

If A1 fails, the claim degrades from "bit-identical by construction" to "empirically close",
and `--reg_compare` (§7) becomes the instrument that quantifies it. The project still lands;
the guarantee weakens. This must be reported, not papered over.

## 4. What is NOT being done, and why

An earlier draft proposed fanning the rigid stage out into per-slide Nextflow tasks
(`REG_FEATURES ×N → REG_GRAPH → REG_MICRO_RIGID ×N`). Investigation rejected this:

1. **Per-slide 2-node VALIS graphs are not faithful.**
   `valis_lib/serial_rigid.py:1240-1277` — `finalize()` computes the common canvas as the
   bounding box over the warped corners of **all** slides, then multiplies **every** slide's
   `M` by the same `crop_T` translation. A 2-node graph yields a different `crop_T`, a
   different canvas `(h, w)`, and therefore a different rescale factor into the 4096 px
   non-rigid stage — a materially different displacement field.
   `crop='reference'` (`bin/utils/valis_config.py:55`) does not rescue this: the crop bbox is
   reference-derived (`valis_lib/registration.py:556-559`) but the canvas it is cropped from
   is all-slides-derived.

2. **On a single machine the fan-out buys ~nothing.** VALIS already parallelizes its
   expensive rigid loops with joblib/pqdm:
   - matching — `valis_lib/serial_rigid.py:580, 645`, `Parallel()(delayed(match_img_obj)(i) ...)`
   - MicroRigidRegistrar — `valis_lib/micro_rigid_registrar.py:298`,
     `pqdm(range(n_tiles), _match_tile, n_jobs=n_cpu)`
   Splitting these across N Nextflow tasks gives each task `cores/N` instead of one task
   getting all cores. Same wall-clock, more overhead, more RAM. The fan-out pays only across
   multiple cluster nodes.

3. **It is the worst option on faithfulness.** Transforms compose through a `to_prev_A` chain
   plus `wiggle_to_ref` (`valis_lib/serial_rigid.py:1045, 1104, 1254, 1596`); reproducing that
   ordering outside VALIS is delicate.

**The one genuinely unparallelized rigid loop** is feature detection —
`valis_lib/serial_rigid.py:502-511`, a plain serial `for` over slides. If measurement shows it
dominating, the targeted fix is a one-hunk seam patch wrapping that loop in the same joblib
`Parallel()` VALIS already uses two functions below. SuperPoint inference is deterministic, so
it stays bit-identical. No Nextflow restructuring. This is deferred, not designed in.

## 5. Architecture

### 5.1 New: `bin/utils/mirage_slide_reader.py`

`MirageVipsSlideReader(SlideReader)` — VALIS-API-compatible, JVM-free.

- Opens with `pyvips.Image.new_from_file(f, access='random', n=-1)` and splits by
  `page-height` into bands. This is the exact inverse of `bin/reg_finalize.py:257-275`
  (`_save_ome_pyvips`), which packs C bands vertically into pages and sets `page-height`.
- Metadata (dimensions, channel names, physical pixel size) parsed from the OME-XML header
  via tifffile — cheap, no JVM.
- Implements `slide2vips(level, series, xywh)`, `slide2image`, `metadata`.
- `can_read(f)` guard: tiled? BigTIFF? band layout as expected? Anything unrecognized returns
  `False`.
- `get_reader_for(f)` factory: returns `MirageVipsSlideReader` when `can_read`, otherwise
  defers to `slide_io.get_slide_reader(f)` with the existing heap sizing. This keeps
  non-mirage inputs and older prior-run references working unchanged.

### 5.2 Injection points

| File | Change |
|---|---|
| `bin/reg_prep.py`, `bin/reg_micro_prep.py` | pass `reader_cls=` into `registrar.register()`; skip `init_jvm()` when all inputs are mirage-readable |
| `bin/reg_finalize.py:224` | use the factory instead of `slide_io.get_slide_reader` |
| `bin/reg_finalize.py` writer | `_save_ome_pyvips` becomes the primary path when JVM-free |
| `bin/utils/valis_config.py` | reader selection + a `no_jvm` mode |

### 5.3 Warp split (the "B" fan-out)

`REG_FINALIZE_FIELD` stops warping. It emits the composed, padded displacement field
(`slide_dxdy.v`, ~134 MB at non-rigid resolution — small enough to stage to every tile task).

```
REG_FINALIZE_FIELD ──► REG_WARP_TILE ×N ──► REG_ASSEMBLE
  (compose field)      (identical warp,     (lazy arrayjoin,
                        then .crop)          write pyramid)
```

- **New** `bin/reg_warp_tile.py` — takes warp state, field, source slide, tile index and grid
  spec; performs the identical warp then `.crop(x, y, w, h)`; writes `tile_<i>.v`.
- **New** `bin/reg_assemble.py` — lazily opens tile files, `arrayjoin`s them, writes the
  tiled pyramidal OME-TIFF via `_save_ome_pyvips`.
- **New** `modules/local/reg_warp_tile.nf`, `modules/local/reg_assemble.nf`.
- `params.reg_warp_tiles = 1` degenerates to one `REG_WARP_TILE` task covering the whole
  canvas, with `REG_ASSEMBLE` reducing to a metadata-preserving passthrough. This is the
  low-resource default and avoids the write-then-reread I/O. `> 1` fans out (the cluster
  case). There is exactly one code path; only the grid size differs.
- `REG_WARP_REF` warps the reference to itself through the same `reg_finalize` machinery and
  must therefore use the same split. It gets the same field-emit → tile → assemble treatment,
  with an identity displacement field.

### 5.4 Resource budget

`params.reg_mem_budget_gb` (default `null` = current cluster behaviour) drives the warp tile
grid size, `maxForks`, `VIPS_CONCURRENCY`, and the `memory` directives.

This also corrects a live mis-sizing found during design: `conf/modules.config:260` sets only
`publishDir` for `REG_NONRIGID|REG_MICRO_PREP`, so `REG_NONRIGID` inherits the
`process_medium` label (`conf/modules.config:44`) and requests **200 GB** for a JVM-free
DeepFlow on an image capped at 4096 px — single-digit GB of real work. `REG_MICRO_PREP`
inherits `process_high` = 300 GB, equal to classic `REGISTER`.

Note on Nextflow selector semantics, verified experimentally during design: `withName`
selectors match as a **substring find**, not a full match. `REG_NONRIGID` therefore also
matches the alias `REG_NONRIGID_MICRO`, and `withName: 'REG_FINALIZE'`
(`conf/modules.config:222`) also applies to `REG_FINALIZE_FIELD`/`_MICRO` before the block at
`:243` overrides it. Harmless today because the values coincide; edits must account for it.

### 5.5 `mode=add_cycle`

Adapter selection moves into a shared helper used by both `subworkflows/local/registration.nf`
and `subworkflows/local/add_cycle.nf` (which currently hardcodes `VALIS_ADAPTER` at
`add_cycle.nf:26, 68`). The prior-run reference is readable by the new reader because
`_save_ome_pyvips` already writes `tile=True, bigtiff=True`; anything else falls back per §5.1.

Because the new path emits no registrar pickle, `reg_qc=2` combined with it **fast-fails in
`ParamUtils`** at both entry points rather than silently producing nothing.

### 5.6 Instrumentation

`REG_PREP` emits per-stage timings (slide load, feature detection, matching, micro-rigid,
2-D dump) into its size-log. After the first real run this identifies which loop to attack,
replacing guesswork about bottlenecks with measurement. This is the input to the deferred
decision in §4.

## 6. Data flow (default separated path, micro on)

```
REG_PREP ──► REG_NONRIGID ──► REG_MICRO_PREP ──► REG_NONRIGID_MICRO ──► REG_FINALIZE_FIELD
(per patient)  (per slide)      (per patient)       (per slide)          (per slide)
                                                                              │
                                                          REG_WARP_TILE ×N ◄──┘
                                                                 │
                                                          REG_ASSEMBLE ──► registered OME-TIFF
```

All stages JVM-free when every input is mirage-readable.

## 7. `--reg_compare`

New `subworkflows/local/reg_compare.nf`. Runs classic `VALIS_ADAPTER` and the new adapter over
the **same** `ch_grouped_multi`, joins outputs by `[patient_id, slide]`, and runs a new
`COMPARE_REGISTRATION` process per slide.

`bin/compare_registration.py` streams both registered slides tile-by-tile (bounded RAM, so it
runs on the same low-resource machine) and emits:

- per-channel `max|Δ|`, `mean|Δ|`, RMSE, and %-pixels-differing, as JSON
- a downsampled diff PNG for visual inspection

Results fold into the existing QC report. Cost is 2× registration; the mode is opt-in.

## 8. Testing

| Test | Asserts | Runs where |
|---|---|---|
| `tests/unit/test_mirage_slide_reader.py` | reader round-trips the pipeline's own writer exactly vs tifffile | CI (`pytest.importorskip` for pyvips, matching `tests/unit/test_tile_grid.py`) |
| `tests/integration/verify_lowmem_bitidentical.py` leg 1 | classic `register.py` == new path, `max|Δ|=0` | VALIS image, `workflow_dispatch` |
| `tests/integration/verify_lowmem_bitidentical.py` leg 2 | tile fan-out == single-process warp, `max|Δ|=0` | same |
| nf-test stubs | `REG_WARP_TILE`, `REG_ASSEMBLE`, `--reg_compare` wiring | CI |

Additionally, wire the existing but currently-unreferenced
`tests/integration/verify_micro_bitidentical.py` into the same `distributed-integration`
job (`.github/workflows/ci.yml:346`). It is the only end-to-end proof against *classic*
rather than against VALIS's own tiler, and today it runs nowhere.

## 9. Out of scope

- `reg_qc=2` on the new path (fast-fails with a clear message).
- Brightfield per-tile `ColorfulStandardizer` parity.
- Non-mirage input formats — they fall back to BioFormats.
- Rigid-stage Nextflow fan-out — rejected in §4, revisit only for multi-node single-patient
  scaling.

## 10. Implementation order

1. Probe **A1** and **A2**. Report results before writing implementation code.
2. `mirage_slide_reader.py` + unit test.
3. Injection into `reg_prep` / `reg_micro_prep` / `reg_finalize`; `verify_lowmem_bitidentical.py` leg 1.
4. Warp split: `reg_warp_tile.py`, `reg_assemble.py`, the two modules, adapter wiring; leg 2.
5. `reg_mem_budget_gb` + the `REG_NONRIGID` / `REG_MICRO_PREP` resource corrections.
6. `add_cycle` adapter selection + `reg_qc=2` fast-fail.
7. `--reg_compare` subworkflow + `compare_registration.py`.
8. CI wiring, including `verify_micro_bitidentical.py`.
