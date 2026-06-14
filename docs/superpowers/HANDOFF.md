# HANDOFF — Distributed VALIS tiled registration (updated 2026-06-14)

## ⏩ Latest state (2026-06-14) — read spec §6.1 + §6.2 + §6.3 first

- ✅ **Task 1 spike DONE** (commit `fb7aae6`): tile externalization is **bit-identical** (`max|Δ|=0`,
  3×3 grid, bk+fwd). Spike kept at `bin/spikes/spike_externalize_tiles.py`.
- ✅ **Phase 1 DONE** (commits `7efd649`, `2d52253`): `bin/utils/tile_grid.py` + `tile_io.py` with
  passing unit tests (run in-image: `docker run ... python3 tests/unit/<f>.py`).
- ✅ **Task 4.5 spike DONE (2026-06-14) → spec §6.3**: Option-A plain-data FINALIZE is **bit-identical**
  (`max|Δ|=0`, all 4 legs: compose / warp / disk-handoff / end-to-end chain) vs whole-image classic, on
  the production-matching config. Spike kept at `bin/spikes/spike_finalize_option_a.py`.
  - 🔑 **Two findings that change Task 7:** (1) `slide_tools.warp_slide` is a **pure plain-data fn** —
    FINALIZE needs **no `Slide`/`Valis` rebuild for the pixels**, just that fn + dumped scalars.
    (2) Production always runs non-rigid on scaled images ⇒ `from_rigid_reg=False` ⇒ the
    `remove_invasive_displacements` compose steps are **NOT executed**; compose = (opt mask) → add ref
    field → (opt mask) → `get_inverse_field`. Gate remove-invasive on `from_rigid_reg` in Task 7.
- ⚠️ **3 blockers found → spec §6.1**: (1) pyvips images are unpicklable ⇒ no cross-process registrar
  handoff; (2) `register()` swallows exceptions ⇒ halt via filesystem, not raise; (3) `ChannelGetter`
  crashes in `process_tile` ⇒ classic tiling broken for fluorescence.
- ✅ **Task 4 DONE (commit `2ae2687`)**: `containers/valis/calc_hook.patch` splits `calc` → dispatch +
  `_calc_tiles`, applied in the Dockerfile (image rebuilt; "hook seam OK"; §6.1 spike still 9/9
  bit-identical). `bin/utils/valis_tiling.py` = the seam module (`install_halt_hook` /
  `install_dump_hook` / `install_read_hook` / `clear_hook` + `_dump_inputs` contract). Smoke-tested.
- ✅ **Task 6 DONE (commit `f7a14ef`)**: `bin/reg_tile.py` — single-tile VALIS `reg_tile`, JVM-free,
  reads the dump contract, `processing_cls=None`. Validated end-to-end vs the in-process loop:
  **bit-identical 9/9 tiles**.
- 🔀 **Architecture = Option A (plain-data handoff), spec §6.2/§6.3.** RAM win = the per-step
  decomposition (no-JVM `REG_TILE` swarm); FINALIZE = compose (gated on `from_rigid_reg`) →
  `pad_displacement` → `slide_tools.warp_slide` (VALIS's streaming warp), all from plain dumped data.
- ‼️ **PREMISE FINDING (spec §6.5):** tiled DeepFlow ≠ whole-image DeepFlow (measured: ~8px max\|Δ\|,
  even for a single 1×1 tile). So "distributed field == whole-image classic" is **impossible** — the
  correct field baseline is the **in-process tiler on identical 2-D images** (already `max|Δ|=0`, §6.1).
  "Bit-identical to classic" holds literally **only for large brightfield** (where classic itself tiles).
  For **fluorescence** there is NO working classic baseline (whole-image is a different algorithm;
  classic-tiled crashes, Blocker 3) — validate vs in-process tiler on the same 2-D images.
- ✅ **ARCHITECTURE RESOLVED with user (spec §6.6) — fix-A + below-threshold fallback to classic.**
  Decisions: process-to-2-D in PREP (fix-A); below VALIS's tiling threshold → classic whole-image
  `REGISTER`. Rationale: `processing_cls=None` on a multichannel tile does `rgb2gray` not DAPI
  (1326-1338), and a guarded ChannelGetter can't map the channel per-tile without `src_f` — so the
  tiler MUST be fed base VALIS's processed 2-D DAPI. fix-A reuses every proven piece, so the result is
  **bit-identical by construction to VALIS's tiler run in-process on VALIS's own processed 2-D image**.
- ✅ **Tasks 5, 7, 8 DONE (commits `040597e`, `c39532d`).**
  - **Task 5 `bin/reg_prep.py`** (fix-A §6.6): force processed-2-D branch, no-op `OpticalFlowWarper`
    to skip DeepFlow (low RAM) while capturing the 2-D images, dump per-slide `tiler_inputs/`
    (via `valis_tiling` halt-on-2-D) + `warp_state.json`. No registrar pickle.
  - **Task 7 `bin/reg_finalize.py`**: `stitch_tiles` → §6.3 compose (gated on `from_rigid_reg`) →
    `pad_displacement` → `slide_tools.warp_slide` → OME-TIFF (pixel-faithful fallback save; the
    ome-types `OMEConverter.to_xml()` path is env-fragile — channel-metadata parity is a Task-10 polish).
  - **Validated END-TO-END:** `PREP → 9× REG_TILE → FINALIZE` on the P001 fluorescence pair produces
    a registered 3-channel OME-TIFF; per-leg bit-identicality already proven (Tasks 6 + 4.5).
  - **Task 8** `modules/local/reg_{prep,tile,finalize}.nf` + `tests/modules/reg_*.nf.test`: all 3
    compile + stubs pass (`nextflow run -stub`, 0 errors with label memory capped). Container =
    `params.reg_dist_container` (MUST be the patched image; published cdgatenbee lacks the seam).
- ▶️ **REMAINING: Task 9 (routing) + Task 10 (verify).** Task 9 = a new `valis_distributed_adapter.nf`
  + branch in `registration.nf` on `params.reg_distributed_tiling`. **Design worked out (do this):**
  - PREP input = same shape as classic `VALIS_ADAPTER` (`[meta, pid, ref_file, all_files, all_metas]`).
  - Fan-out: `REG_PREP.out.prepped` (`[pid, prep_dir, all_metas]`) → `flatMap` that, per slide subdir,
    reads `tiler_inputs/manifest.json` (`new JsonSlurper().parse(...)`) for `n_tiles` and emits
    `[pid, slide, file(tiler_inputs), i]` for `i in 0..<n_tiles` → `REG_TILE`.
  - Fan-in: `REG_TILE.out.tiles.groupTuple(by:[0,1])` → flatten tile `.v` list; rejoin per slide with
    `tiler_inputs` + `warp_state.json` (from prep) + `src_slide` (from `all_items`) → `REG_FINALIZE`.
  - Output: convert `REG_FINALIZE.out.registered` → `[meta, file]` (match slide→meta by channel sig,
    like the classic adapter). **⚠️ MUST also emit the REFERENCE** warped-to-itself (classic warps all
    slides incl. ref; downstream QC compares moving-registered vs reference-registered in the same
    cropped space) — either run FINALIZE on the ref with a zero non-rigid field, or warp it via
    `slide_tools.warp_slide` with identity dxdy. Don't forget this or QC/coords break.
  - **Below-threshold fallback (user decision):** route patients/inputs below VALIS's tiling threshold
    to the classic `VALIS_ADAPTER`; only tile above it.
  - **Params to add** (nextflow.config + schema): `reg_distributed_tiling` (false), `reg_dist_container`,
    `reg_dist_tile_wh` (512), `reg_dist_tile_buffer` (100), `reg_max_non_rigid_dim`, `reg_max_processed_dim`,
    `reg_dist_sub_threshold` ('auto'), `reg_dist_tiles_per_task` (1).
  - **Cannot be fully validated locally** — needs the patched image PUBLISHED + a real pipeline run;
    that's why it wasn't written blind. Stub-test the adapter, then real-test once the image is on GHCR.
  - **Task 10** — verify distributed end-to-end == in-process tiler on the same 2-D images
    (fluorescence) + == classic for large brightfield; large fixture. Also finish OME channel-metadata
    parity in `reg_finalize.py` (the `to_xml()` fallback).
  - Follow-up (§6.6): exact base-VALIS-*tiled-brightfield* parity needs per-tile `ColorfulStandardizer`.
- ▶️ **Then remaining:** Task 5 (`reg_prep.py`), Task 7 (`reg_finalize.py`, per §6.3 — compose gated
  on `from_rigid_reg` → `pad_displacement` → `slide_tools.warp_slide`, no Slide rebuild), Tasks 8-9
  (NF processes + routing), Task 10 (bit-identical verification). VALIS source is at `/tmp/valis_src/`
  (re-extract from `mirage-valis:1.0.0` if gone). (Sections below are the original 2026-06-13 handoff,
  still valid for environment/build context.)

## How to restart with full context (do this first)

```bash
# 1. Enter the worktree (all work lives here, NOT on main — main has unrelated uncommitted work)
cd ~/.config/superpowers/worktrees/mirage/valis-tiled-parallel
git status && git log --oneline -12        # branch: valis-tiled-parallel

# 2. Read these, in order (they ARE the context — self-contained):
#    docs/superpowers/specs/2026-06-13-valis-tiled-parallel-registration-design.md   (design + decisions)
#    docs/superpowers/plans/2026-06-13-valis-tiled-parallel-registration.md          (task-by-task plan)
#    this file

# 3. Confirm the built reference image still exists:
docker images | grep mirage-valis     # expect: mirage-valis:1.0.0  (~10.7GB)
docker run --rm mirage-valis:1.0.0 python3 -c "import valis;print(valis.__version__)"   # expect 1.0.0
```

To resume in a fresh Claude session, say e.g.:
> "Resume the valis-tiled-parallel work — read docs/superpowers/HANDOFF.md in the worktree, then run Task 1 (the spike)."

## What this is

An **opt-in** Nextflow path (`params.reg_distributed_tiling`) that lifts VALIS 1.0.0's in-process
non-rigid tile loop into Nextflow processes, **bit-identical** to classic VALIS in the tiling
regime, decomposed per-step for low-budget clusters. Default pipeline behavior is unchanged.

## Decisions locked (with the user)

- **Reference = `valis-wsi==1.0.0`** (the version in the deployed image; `valis_lib/` is byte-identical to it). NOT 1.2.0 — different tile kernel.
- **Approach A** (per-tile fan-out), **opt-in** via param, **bit-identical** target.
- **Strategy 2** (inject precomputed tiles back into VALIS; keep all composition in VALIS).
- **Seam = Option B**: explicit `EXTERNAL_TILE_HOOK` patch over pip VALIS (`containers/valis/calc_hook.patch`), NOT runtime monkeypatch. Default-None ⇒ classic path byte-identical.
- **§5C per-step decomposition for low-budget**: `RIGID_PREP` (JVM, rigid only, halt before tiles) → `REG_TILE` (NO JVM, ~1-2GB, the cheap-node swarm) → `REG_FINALIZE` (JVM, stitch+compose+micro+warp).
- **Don't tile rigid/feature/matching** (§5B): they run downsampled (~2k px), tiny RAM regardless of slide size — tiling buys nothing and seams the alignment. Only non-rigid+micro scale → only they are tiled.
- **Micro-registration (§5A)**: 2nd non-rigid pass (tile_wh=2048, updating mode); when on, `REG_FINALIZE` runs `register_micro()` in-process (Option 1) so it isn't silently dropped.
- **DeepFlow-only** precondition (SimpleElastix backend is RNG-dependent → not parallel-safe).

## State / what's done

- ✅ Spec + plan written, committed, **adversarially verified** (3 reviewers; params + regime boundary confirmed exact against 1.0.0 source).
- ✅ `mirage-valis:1.0.0` image **built & verified** (valis 1.0.0, pyvips 2.2.3, cv2 4.6.0 DeepFlow True, torch CPU). Took 4 real Dockerfile fixes (meson order, `SETUPTOOLS_USE_DISTUTILS=stdlib`, pip retries, version pin) — all committed.
- ✅ Dockerfile pinned `==1.0.0`. (calc_hook.patch is in the plan but NOT yet created/applied — that's Task 4.)
- ⬜ **No implementation code written yet** (no bin/ scripts, no modules/). Plan is the next thing to execute.

## NEXT STEP — Task 1 spike (the make-or-break gate)

Prove externalize-and-resume is bit-identical, on the built image. Run it **directly** (it's
exploratory VALIS-internals debugging, not a clean spec task — don't delegate to a subagent):

```bash
docker run --rm -v "$PWD":/work -w /work mirage-valis:1.0.0 python3 bin/spikes/spike_externalize_tiles.py
```

It must prove: (a) read-mode stitched `bk_dxdy` == unmodified-VALIS baseline (exact), and
(b) §5C halt → pickle registrar → resume in a fresh process == baseline. See plan Task 1, Steps 1-6.
- If both pass → proceed to Phase 1 (Tasks 2-3 TDD kernels), then dispatch Tasks 2-10 to subagents per subagent-driven-development.
- If pickle/resume fails → fall back to `install_dump_hook` (PREP computes tiles inline); record in spec §6.1.

## Gotchas

- Work is in a **worktree**, not main. Don't lose it: `git worktree list`.
- Building the Dockerfile fresh resolves valis to 1.2.0 (unpinned upstream) — always keep the `==1.0.0` pin.
- The image is arm64 (Apple Silicon); fine because the bit-identical comparison runs classic-vs-distributed in the *same* image.
- Test data: `python tests/testdata/generate_complete_testdata.py` before running anything.
