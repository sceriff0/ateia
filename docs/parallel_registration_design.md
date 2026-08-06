# STARE — a fully-parallel, tiled, laptop-friendly registration method for mirage

**Status:** implemented on branch `feat/tiled-registration` (Phases 1–2 + Nextflow wiring, 56
Python tests, JVM-free stub run green). Remaining: reg_qc=2 seg-QC Nextflow dispatch, the slim
container, real-data accuracy validation, and the optional per-tile Nextflow fan-out (§5). See the
status box after §10.
**Working name:** **STARE** — *STar-Anchored Registration with Error (TRE)*.
**Method id:** `registration_method = 'tiled'`.
**Companion:** `docs/parallel_registration_research.md` — primary-source notes on ASHLAR & VALIS.

> Naming note: an earlier draft of this file called the method "PARSEC" and built it around a
> spanning **tree**. That was a misread of the request — the ask was for VALIS-style **TRE**
> (*Target Registration Error*), not a *tree*. There are **no trees** in this design; a fixed
> reference removes the ordering problem that ASHLAR/VALIS need trees for. See §3.

---

## 1. Goal & constraints

A second `registration_method` alongside `valis` that is:

1. **Fully parallel at the Nextflow level** — every expensive unit is an independent process
   (per slide *and per tile*), so a cluster runs them all at once and a laptop runs a few at a
   time. No monolithic per-patient task.
2. **The default choice for laptops / low-end machines** — **every process fits in ≤8 GB**, no
   JVM, no BioFormats, no whole-slide-in-RAM step. The tiling and stitching processes are
   themselves low-memory and stream.
3. **Native TRE** — emits a VALIS-style Target Registration Error per slide *and* a spatial TRE
   heatmap, as a free byproduct of registration.
4. **reg_qc=2 compatible** — same staged segmentation-overlap QC (`native → rigid → refined`),
   with *zero changes to the QC scorer*.
5. **Non-negative output** — warped/stitched pixels are never negative (protects quantification).
6. **A drop-in adapter** — same subworkflow channel contract as `VALIS_ADAPTER`, selected by
   `params.registration_method`.

ASHLAR is the reference point (phase-correlation + tiled mosaic). STARE reuses ASHLAR's tile +
phase-correlation machinery but, because mirage supplies a fixed reference, replaces ASHLAR's
global MST solve with **reference-anchoring** — every tile lands in absolute coordinates on its
own. That is the novel core (§9).

---

## 2. Why VALIS is the wrong tool on a laptop (evidence from this repo)

| Property of current `REGISTER` | Where | Why it hurts low-end machines |
|---|---|---|
| One monolithic fan-in task per patient, **`process_high` = 200 + 100·attempt GB** (attempt 1 asks **~300 GB**), 8 CPU, 12 h | `register.nf:15` + `conf/modules.config:24-27` | Categorically cluster-only; the only parallelism is *across patients*. |
| JVM heap 32 GB base, +16 GB per retry | `register.nf:57` | BioFormats needs a huge JVM; a laptop has 8–16 GB total. |
| Loads *all* slides at once to build the transform graph | `valis_adapter.nf:8`, `register.py:442` | Peak RAM ≈ all slides in one address space. |
| Sequential per-slide warp | `register.py:843` | The embarrassingly-parallel part is serialized. |
| Can emit **negative pixels**, patched downstream | `register.py:893` (clipped by `split_multichannel.py`) | Overshoot corrupts quantification unless clipped after the fact. |

STARE removes every row: per-tile ≤8 GB tasks, no JVM (pure NumPy/OpenCV/scikit-image/tifffile),
tiles fan out, and non-negativity is guaranteed at the source (§7).

---

## 3. Core concept: reference-anchored parallelism, no trees

ASHLAR and VALIS build trees/orderings because **they must discover a reference and a
registration order from the data** — ASHLAR chains tiles via a minimum spanning tree
(`build_spanning_tree`), VALIS orders whole images via a hierarchical-clustering dendrogram
(`serial_rigid.py`: `order_Dmat` = `fastcluster.linkage` + `optimal_leaf_ordering`).

Mirage hands you the reference explicitly (`is_reference` from the CSV, `registration.nf:117`),
and cyclic-IF re-stains the **same physical section** across cycles. Two consequences:

- **The reference is the global coordinate frame.** Any slide — or any *tile* — that registers to
  the corresponding reference region gets **absolute coordinates for free**. No inter-image or
  inter-tile reconciliation, hence **no tree, no global solve.**
- **Topology is a star:** every moving slide → the reference, independently and in parallel.

The whole design is: make that star *tiled* (for the ≤8 GB memory bound) and *TRE-instrumented*
(for quality), while keeping every unit independent.

---

## 4. The enabling insight: reg_qc is already method-agnostic

The reg_qc=2 scorer in `bin/warp_seg_qc.py` **never imports VALIS**. Its core — `run()` (`:203`),
`score_stage()` (`:154`), `plan_stages()` (`:113`) — works entirely through an *injected* callable
`warp(slide_name, xy, stage) -> warped_xy`, and `write_report()` already accepts an injected
`warp` + `stages` and skips the VALIS loader when they are provided (`warp_seg_qc.py:377-398`).

So STARE does not reimplement QC. It supplies:

1. A per-slide **transform manifest** — `M₀` (global rigid) + a **control-point grid** for the
   mesh field (KB). Replaces the VALIS `registrar.pickle`.
2. A **warper module** `bin/utils/tiled_stage_warp.py` exposing
   `make_warper(manifest) -> warp(name, xy, stage)` where
   `warp(name, xy, 'rigid') = M₀·xy` and `warp(name, xy, 'refined') = M₀·xy + F(xy)`, with `F`
   the smooth mesh field (§6). Pure NumPy — **no JVM**.
3. The stage plan `[native, rigid, refined]`.

Everything else (`warp_seg_qc.py`, `seg_qc_geojson.py`, `utils/cell_pairs.py`) is reused
byte-for-byte. There is no `micro` stage and no destructive composition, so the VALIS
`stage_checkpoint` machinery (`register.py:660`) is simply not needed — `ch_stage_checkpoint` is
`Channel.empty()`.

**Per-slide variable separability (honest wrinkle):** because refinement is TRE-gated (§5), a
slide (or region) that stayed rigid reports `[native, rigid]`; a refined one reports
`[native, rigid, refined]`. `plan_stages` already handles variable separability — this is more
honest than faking an identity `refined` stage.

Exact reg_qc=2 artifact STARE must keep producing (per moving slide):
`<outdir>/<patient>/qc/registration/<patient>_<slide>_seg_qc.json` with keys `iou_mean`,
`iou_p10/p50/p90`, `frac_iou_ge_0.5`, `displacement_px_p50/p90/max` (+ `_um`), `dice_matched`,
`delta_vs_anchor`, `stages_separable`, `matching{…}`, `counts{…}`. Reusing the scorer gives this
shape for free.

---

## 5. Architecture — all processes ≤8 GB, all Nextflow-parallel

```
per patient  (patients already parallel)
 └─ per moving slide  (STAR: each slide → the fixed reference, independent)
     COARSE    thumbnail feature-align (ORB + RANSAC) → global rigid M₀      ~1–2 GB
               # features absorb inter-cycle ROTATION/scale; cheap, per slide
     TILE      stream tiles from the tiled OME-TIFF (region reads, halo)      ~1 GB
               # low-mem split; no whole-slide load
     REG_TILE  per tile ∥: moving DAPI tile + reference region (placed by M₀) ≤8 GB
               phase-correlation residual  # near-TRANSLATION after M₀ → ASHLAR's kernel fits
               → one control-point displacement dᵢ at tile centre cᵢ
               → per-tile TRE;  TRE-gated: only high-TRE tiles get a local non-rigid nudge
     WARP_TILE per tile ∥: sample smooth field F = interp({cᵢ→dᵢ}); BILINEAR resample ≤8 GB
               # all channels; non-negative by construction (§7)
     STITCH    write pyramidal OME-TIFF tile-by-tile; feather-blend halos      ≤8 GB
               # low-mem merge; never whole slide in RAM
 └─ emit: registered slide + intrinsic TRE (per-slide table + spatial heatmap)
```

**Primitive split (falls out of the architecture):** COARSE uses feature matching (ORB/RANSAC)
because inter-cycle repositioning can carry rotation and small scale; after M₀ the per-tile
residual is near-pure-translation, so REG_TILE uses **phase-correlation** (ASHLAR's whitened,
Hann-windowed `phase_cross_correlation`) — cheapest possible, no keypoints needed.

**Memory sanity-check (8 GB is generous):** REG_TILE loads only **DAPI** for a 4096² moving tile
(~32 MB) + reference region + halo (~64 MB). WARP_TILE loads that tile's *all* channels
(4096² × ~10 ch × 2 B ≈ 320 MB). STITCH holds a few tiles for blending. Nothing approaches 8 GB;
8192² tiles are safe if you want fewer tasks. Tile size *is* the mesh-grid resolution knob:
smaller tiles → finer non-rigid but more tasks.

---

## 6. TRE + seam continuity (the two quality mechanisms)

**TRE — intrinsic, VALIS `error_df` semantics.** Each REG_TILE scores the residual of the
phase-correlation match *after* applying its transform — a Target Registration Error per tile,
aggregated to a per-slide table (mean/percentiles, in px and µm) and a **spatial heatmap** (one
value per tile — strictly richer than VALIS's single `error_df` number).

**Seam continuity — smooth mesh/grid warp.** Independent per-tile fields would tear cells at tile
boundaries (the problem ASHLAR's MST solves). Instead each tile contributes **one displacement
control-point** `dᵢ` at its centre `cᵢ`; the actual warp is a *single continuous field*
`F(x) = interp({cᵢ → dᵢ})` (bilinear over the control grid, or thin-plate spline). Seam-free by
construction. WARP_TILE and the reg_qc=2 warper sample the **same** `F`, so QC measures exactly
what shipped. Manifest = `M₀` + control grid (KB). TRE-gated tiles that don't refine just
contribute `dᵢ = 0` — the interpolation stays smooth.

---

## 7. Non-negative output (guaranteed, not patched)

Downstream quantification (per-cell mean/median marker intensity) is corrupted by negative
pixels. STARE never generates them:

- **WARP_TILE uses bilinear resampling only.** Bilinear is a convex combination of the four
  neighbouring source pixels, so the result lies in `[min, max]` of non-negative inputs → never
  negative. **Bicubic/Lanczos are forbidden** here: their overshoot (ringing) manufactures
  negatives. This matches the repo's own guidance (`register.py:1046`).
- **STITCH feather-blend is convex** (weights ≥0, sum to 1) → preserves non-negativity across
  halos.
- **Belt-and-suspenders:** after resample+blend, `clamp(0, dtype_max)` and preserve the source
  dtype (uint16), catching any floating-point rounding to −ε.
- The mesh field carries *signed displacements* (coordinates) — unrelated to pixel values, and
  correct.

Unlike the VALIS path, which relies on `split_multichannel.py` to clip negatives *after* warping
(`register.py:893`), STARE's output is non-negative before it is ever written.

---

## 8. Nextflow wiring (drop-in adapter)

The hook exists: `params.registration_method` is defined (`nextflow.config:61`) and validated
via its `nextflow_schema.json` enum (nf-schema, not `ParamUtils`), but `registration.nf:182` hardcodes
`VALIS_ADAPTER`. Add the value `'tiled'` to the validator and branch:

```groovy
// subworkflows/local/registration.nf, STEP 3
if (params.registration_method == 'tiled') {
    TILED_ADAPTER(ch_grouped_multi)                 // new: subworkflows/local/adapters/tiled_adapter.nf
    ch_registered       = TILED_ADAPTER.out.registered
    ch_registrar_pickle = TILED_ADAPTER.out.manifest   // transform manifest (M₀ + control grid), not a pickle
    ch_stage_checkpoint = Channel.empty()              // STARE needs none (§4)
    …
} else {
    VALIS_ADAPTER(ch_grouped_multi)
    …
}
```

`TILED_ADAPTER` emits the **identical channel contract** (`registered [meta,file]`,
`registrar [pid, manifest]`, `stage_checkpoint` empty, `size_logs`, `versions`, `summary`) so
nothing downstream changes. Internally it is the fan-out of §5.

New modules: `modules/local/tiled_coarse.nf`, `tiled_tile.nf`, `tiled_reg_tile.nf`,
`tiled_warp_tile.nf`, `tiled_stitch.nf`. New bin scripts: `tiled_coarse.py`, `tiled_tile.py`,
`tiled_reg_tile.py`, `tiled_warp_tile.py`, `tiled_stitch.py`, plus
`bin/utils/tiled_stage_warp.py` (the QC seam) and `bin/utils/mesh_field.py` (shared field
interpolation — used by WARP_TILE *and* the warper, single source of truth).

reg_qc=2 dispatch: add `--method tiled` to `warp_seg_qc.py` so it injects
`tiled_stage_warp.make_warper(manifest)` + `stages=[native,rigid,refined]` instead of the VALIS
loader (`warp_seg_qc.py:105,377`). The scorer is untouched.

Container: a slim `python + opencv + scikit-image + tifffile + numpy + scipy` image — **no JVM,
no libvips-from-source**.

Resource labels: STARE tasks genuinely need only a few GB, but the standard labels are
cluster-sized (`process_low`=32 GB, `process_medium`=200 GB in `conf/modules.config`). Ship
dedicated lean `withName:'TILED_*'` overrides (2–8 GB) or pair with a memory-capped profile like
`conf/test.config` (pins `process_high`=6 GB). Under such a profile a 4-core/16 GB laptop runs
2–4 tiles concurrently and the pipeline *completes* instead of OOM-killing.

> Exec-bit rule (CLAUDE.md): every name-invoked `bin/tiled_*.py` must be
> `git update-index --chmod=+x` → mode `100755`, or it fails exit 126 on the cluster. Import-only
> `bin/utils/*.py` stay `100644`.

---

## 9. What is genuinely new here

1. **Reference-anchored tiled registration — tiling without a global solve.** ASHLAR must run an
   MST to make tiles consistent because it has no reference; STARE registers each tile to the
   reference region, so absolute position is free and there is no inter-tile solve. Tiling
   composes with the star instead of needing a tree.
2. **Registration-as-a-DAG-of-≤8 GB-processes.** The archived tiled path tiled only the *warp*
   (monolithic VALIS `REG_PREP`); STARE tiles the *registration estimation* itself, JVM-free.
3. **Intrinsic per-tile TRE → a spatial error heatmap** that doubles as the refinement gate —
   quality metric and control signal are the same object.
4. **Non-negativity by construction** (convex resample + convex blend), not post-hoc clipping.

---

## 10. Implementation plan

1. **QC seam first (lowest risk).** Write `bin/utils/tiled_stage_warp.py` + `mesh_field.py` and a
   fake manifest; unit-test that `warp_seg_qc.run()` scores a `[native,rigid,refined]` plan
   through it (the scorer is already injectable — `warp_seg_qc.py:377`). Proves reg_qc=2 works
   before any registration exists.
2. **Rigid core.** `tiled_coarse.py` (M₀) + `tiled_tile.py` + `tiled_reg_tile.py` (phase-corr
   residual, TRE, no non-rigid yet) + `tiled_warp_tile.py` (bilinear) + `tiled_stitch.py`. Wire
   `TILED_ADAPTER` and the `registration.nf` branch. Validate on the test profile
   (`nextflow run . -profile test,docker -stub`) against known VALIS output. Assert non-negativity.
3. **Mesh non-rigid + reg_qc=2 end-to-end.** Add TRE-gated control-point refinement, the smooth
   field, and the `refined` stage; dispatch `warp_seg_qc.py --method tiled`.
4. **TRE outputs.** Per-slide table + spatial heatmap.
5. **nf-test** modules, exec-bit the bin scripts, CI stub coverage, `low`/`high` presets.

## Risks / open questions

- **Accuracy ceiling.** A coarse mesh non-rigid is weaker than VALIS optical-flow micro-
  registration. STARE is deliberately the *fast/low-mem* option; VALIS stays the *high-accuracy*
  option. The intrinsic TRE + reg_qc=2 quantify the gap so a user knows when to escalate.
- **Mesh resolution ↔ parallelism tradeoff.** Finer control grid = better non-rigid but more
  tiles/tasks. Tile size is the knob; document the tradeoff, don't hide it.
- **Archived path lessons.** `archive/tiled-valis-2026-07-24` (`REG_PREP→REG_WARP→REG_ASSEMBLE`,
  patched VALIS container) was removed 2026-07-24 — appears to be scope/publication cleanup, not a
  viability failure. STARE differs fundamentally (JVM-free, per-*tile registration*, mesh-warp
  continuity). Confirm with the author whether any removal reason must be designed around.
- **Sparse-fluorescence COARSE.** ORB on DAPI needs enough keypoints; phase-correlation on the
  thumbnail is the M₀ fallback for very sparse tissue.
- **Output frame.** Anchor to the reference slide's native grid (identity for the reference), so
  registered pixel coordinates match what postprocessing/segmentation expects.
- **OME channel manifest.** STITCH must still emit `channels_manifest.json` (filename → OME
  channel names) so `TILED_ADAPTER` matches registered files back to meta by channel signature
  (`valis_adapter.nf:76-113`). Reuse `create_channels_manifest.py`.

## Implementation status (branch `feat/tiled-registration`)

**Done & verified**
- **Phase 1 — reg_qc=2 seam:** `bin/utils/mesh_field.py` (smooth field + non-negative bilinear
  resampler), `bin/utils/tiled_stage_warp.py` (`make_warper`). Unit-proven that the existing
  `warp_seg_qc` scorer runs a `[native, rigid, refined]` plan through the tiled warper unchanged.
- **Phase 2 — rigid core:** `tile_grid`, `coarse_align` (ORB+RANSAC M0 + residual TRE),
  `tile_residual` (whitened/Hann phase-corr), `tiled_warp` (inverse-map bilinear, non-negative),
  `tiled_manifest` (TRE-gated control grid), `tiled_pipeline.register_slide` (end-to-end). An
  end-to-end test realigns a synthetically warped slide (corr > 0.9), a pure shift needs no mesh,
  a non-rigid warp is captured by the mesh.
- **CLI:** `bin/tiled_register.py` (100755) — real OME-TIFF I/O, smoke-tested.
- **Nextflow wiring:** `modules/local/tiled_register.nf`, `subworkflows/local/adapters/tiled_adapter.nf`,
  the `registration.nf` method branch, `validateRegistrationMethod += 'tiled'`, `reg_tiled_*`
  params, and a lean 8 GB `TILED_REGISTER` resource block. **Stub run green** end-to-end, JVM-free.
- **reg_qc=2 seg-QC dispatch (done):** `warp_seg_qc.py --method tiled` builds the warper from the
  STARE manifest (no JVM), and `WARP_SEG_QC_TILED` + the valis/tiled dispatch branch in
  `subworkflows/local/seg_qc.nf` (called from `registration.nf`) feed it one manifest per moving
  slide. **Stub run green at reg_qc=2** — emits the `native/rigid/refined`
  `_seg_qc.json`. Unit-tested through the real CLI `main()`.
- **Slim container (done):** `containers/tiled/` (`python:3.11-slim` + numpy/scipy/scikit-image/
  tifffile, no JVM/libvips/GPU). **Built and verified locally** — the CLIs import and run
  in-container; **~438 MB** vs the multi-GB VALIS image. Added to the `build-images.yml` matrix.
- **Intrinsic TRE (done — VALIS-analogous, emitted by both paths):** `_tre.json` carries
  `coarse_tre_px` (rigid feature-fit residual, like VALIS's rigid error), a per-tile `rigid_tre_px`
  **spatial heatmap** VALIS doesn't have, and — in the default monolithic path — `residual_after_px`,
  the per-tile residual *after* the mesh (STARE's post-registration final-accuracy number, the
  analogue of VALIS's non-rigid error; test: it beats the rigid residual). Built by the shared
  `bin/utils/tre_report.py`; the fan-out (`TILED_SOLVE`) now emits the rigid spatial heatmap too
  (previously dropped). The fan-out's final-accuracy residual comes from the reg_benchmark harness.
- **Final QC-report integration (done):** the `_tre.json` already flowed into the report's
  `registration_tre/` input; `generate_qc_report.py` now renders it as a "Registration Accuracy
  (STARE Tiled TRE)" subsection — a per-slide table (coarse / rigid p50-p90 / post-refinement final
  p50-p90 / refined / tiles) plus a **per-tile SVG heatmap** of the spatial TRE, sitting alongside
  the VALIS rTRE, feature-distance, and seg-QC tables. Unit-tested.
- **Accuracy harness (done):** `bin/utils/reg_benchmark.py` + `bin/registration_benchmark.py` —
  a ground-truth-free residual-TRE + correlation metric that runs on any method's output, so
  VALIS vs tiled is a direct number-to-number comparison on the same slide. Validated on synthetic
  ground truth (STARE drops the residual TRE below 2 px; a pure 11.66 px shift is fully removed).
- **Per-tile Nextflow fan-out (done):** `reg_tiled_fanout=true` switches the adapter to
  `TILED_COARSE → TILED_REG_TILE (one task per tile) → TILED_SOLVE → TILED_STITCH` — the
  little-process-per-tile design, all JVM-free. `warp_image` gained an `out_origin` so each tile
  task warps only its window (and the stitch warps in row strips). The fan-out chain is proven to
  compose into a correct registration end-to-end (synthetic ground truth), and the DAG is **stub
  green at reg_qc 1 and 2**; the default (`false`) monolithic path is unchanged.
- **Streaming gigapixel stitch (done):** `TILED_STITCH` no longer materialises the moving slide or
  the output. It reads only each output tile's source pixels (`source_region` inverse-map + a lazy
  `zarr` region read via `tifffile` `aszarr`), warps that tile (`warp_image` `out_origin`+`src_origin`),
  and writes it straight to a tiled OME-TIFF. Peak memory is one source crop + one output tile per
  channel. Proven bit-identical to the whole-image warp (±1 rounding); container gains `zarr`
  (amd64 image built & verified, 482 MB).
- **~67 Python tests passing; ruff clean.**

**Convention settled during implementation:** the mesh lives in the **reference frame** (sampled
at the rigid position `M0·xy`), which the tiled implementation produces naturally and which gives
a decoupled warp inverse. §5/§6 describe this.

**Remaining**
- **Run the accuracy harness on real WSIs vs VALIS** (operational — the metric and tooling are in
  place; this is executing them on real slides + a VALIS run, which needs the cluster).
- **nf-test** module + integration coverage (the Python cores and the stub DAG are covered; native
  nf-test cases for the new processes would round it out).
```
