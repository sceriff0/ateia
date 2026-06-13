# HANDOFF — Distributed VALIS tiled registration (paused 2026-06-13)

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
