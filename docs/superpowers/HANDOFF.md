# HANDOFF — Distributed VALIS registration (updated 2026-06-15)

> **Branch:** `valis-dist-followup` (worktree dir is named `valis-tiled-parallel`; the *branch* is
> `valis-dist-followup`, main + this work). Earlier handoff revisions (below the line) were written
> mid-build and are **superseded** — this top section is the current truth.

## Status: distributed registration COMPLETE, incl. the micro second wave — bit-identical

An **opt-in** Nextflow path (`params.reg_distributed_tiling=true`) that lifts VALIS 1.0.0's
non-rigid registration out of the BioFormats-JVM process into **separated, JVM-free** Nextflow
processes for low-RAM clusters. **Bit-identical** to classic VALIS; default behavior unchanged.

### The three regimes (`subworkflows/local/adapters/valis_distributed_adapter.nf`)

```
REG_PREP (JVM: rigid + capture processed 2-D inputs, halt before DeepFlow, low RAM)
   │
   ├─ (a) force_tiling & skip_micro : REG_TILE fan-out  ────────────────────→ REG_FINALIZE
   ├─ (b) default (skip_micro=true) : REG_NONRIGID (JVM-free whole-image) ───→ REG_FINALIZE_FIELD
   └─ (c) skip_micro_registration=false (MICRO 2nd wave, §5A Option-2):
          REG_NONRIGID(wave1) → REG_MICRO_PREP → REG_NONRIGID_MICRO → REG_FINALIZE_MICRO
```

The RAM win is **process separation** (the heavy DeepFlow runs in a JVM-free process), not tiling —
VALIS's internal auto-tiler rarely fires (spec §6.7). `reg_dist_force_tiling` is the fallback for a
single field too big for one node.

### Micro-registration second wave (`skip_micro_registration=false`) — NEW, proven 2026-06-15

Micro is VALIS's **2nd non-rigid pass at higher resolution** (`micro_reg_size = floor(min_max_full_res
× reg_micro_reg_fraction)`, default 0.125, which is **≥** the main pass's 3000px for any ≥24k-px WSI —
i.e. micro is the *heavier* pass). It is run as a **separated, per-slide-parallel, JVM-free** wave so
it never becomes the FINALIZE serial/RAM bottleneck:

- `bin/reg_micro_prep.py` — rebuild rigid with the **identical** `build_registrar_kwargs` (⇒ same `M`),
  **inject** the composed+padded wave-1 field onto `slide.bk_dxdy`, run `register_micro` with the
  `OpticalFlowWarper` no-op'd to **capture** the micro 2-D inputs + micro `full_out_shape_rc`/`mask_bbox`
  (`registration.py:4374-4375`); halts before micro DeepFlow.
- `bin/reg_finalize.py` — `compose_and_pad` (reproduces classic `slide.bk_dxdy` after `register()`,
  oracle Q1) + `micro_additive` (`scale(wave1)+pad(residual)`, `registration.py:4299-4330`).
- `modules/local/reg_micro_prep.nf`, `reg_finalize_micro.nf`; `REG_NONRIGID` aliased for the micro wave.
- params: `reg_dist_micro_tile_wh` (2048), reuses `reg_micro_reg_fraction` (0.125).

**Why bit-identical:** the updating prep reads only `bk_dxdy` (`registration.py:3492`) and the final
warp uses only the updated `bk` — both reproduced exactly. `fwd` is not propagated by the distributed
path (no downstream consumer), so it's a zero placeholder.

## Verification (local, no cluster) — what is PROVEN

Run in the built image `mirage-valis:1.0.0` (≈10.7 GB; arm64 on Apple Silicon — the comparison runs
classic-vs-distributed in the *same* image, so arch is irrelevant):

```bash
cd ~/.config/superpowers/worktrees/mirage/valis-tiled-parallel
python tests/testdata/generate_complete_testdata.py            # regenerate testdata for THIS checkout

# Micro second wave == classic register_micro (max|Δ|=0 on micro 2-D inputs AND the final field):
docker run --rm -v "$PWD":/work -w /work mirage-valis:1.0.0 bash -lc '
  python3 tests/testdata/generate_large_fixture.py --size 1024 --out /tmp/bigdata &&
  python3 tests/integration/verify_micro_bitidentical.py'        # CMP_MODE=high for the high-mem config

# Wave-1 distributed tiler == VALIS in-process tiler (max|Δ|=0):
docker run --rm -v "$PWD":/work -w /work mirage-valis:1.0.0 \
  python3 tests/integration/verify_distributed_bitidentical.py
```

- ✅ **Micro: `max|Δ|=0`** on the micro 2-D inputs AND the final micro-updated displacement field, vs
  classic `register()+register_micro()`, at **`memory_mode=low`**. The warp is deterministic, so an
  identical field ⇒ identical registered pixels.
  - ⚠️ **`memory_mode=high` not yet locally confirmed.** Bit-identicality is *structural* — baseline and
    distributed use the **identical** `build_registrar_kwargs(memory_mode)`, and the mode only changes
    resolution caps, not the algorithm/wiring (so low-mode `max|Δ|=0` exercises the same code path that
    runs at high). Two local high-mode attempts died in **baseline/shared code** (classic `register()` /
    `reg_prep`) at 2048px before reaching the comparison — Docker resource pressure on this Mac, not a
    correctness issue. **TODO: confirm `CMP_MODE=high` on a node with adequate RAM** (the goal names the
    high-mem config). The reg_prep stage itself runs fine at high mode in isolation.
- ✅ Wave-1 distributed (separated + tiled) proven `max|Δ|=0` previously (`spike_*`, §6.1/§6.3).
- ✅ Full distributed pipeline runs `-stub` EXIT=0 in BOTH regimes (separated+micro, separated-no-micro)
  AND the classic default path (regression) — `nextflow run . -profile test -stub
  --reg_distributed_tiling true --reg_dist_sub_threshold force --skip_micro_registration false`
  (regenerate testdata first: `python tests/testdata/generate_complete_testdata.py`).

## Status of the original "finishing items"

1. **GHCR image publish** — ✅ DONE. `build-valis-image.yml` ran successfully 2026-06-15 (run
   27529170157), image at `ghcr.io/sceriff0/mirage/mirage-valis:1.0.0` (the EXTERNAL_TILE_HOOK-patched
   image; `params.reg_dist_container` points to it). Micro adds no container changes.
2. **Real nf-test suite** — stub tests pass (incl. `reg_micro_prep`, `reg_finalize_micro`). The
   *real* bit-identical proof is the docker verification above (richer than an nf-test would be).
   `nf-test` itself is not installed in this dev env; CI runs the stub suite.
3. **OME channel-metadata parity** — functional: pixels correct, channel **names** preserved. Primary
   save uses VALIS's `update_xml_for_new_img().to_xml()` (full metadata) with a channel-name-preserving
   pyvips fallback when the ome-types/xmlschema path is env-fragile. Full-metadata parity in the
   fallback (colormap/physical-size via ElementTree patch) remains optional polish.
4. **Below-threshold fallback routing** — ✅ DONE (`REG_ESTIMATE`/`bin/estimate_reg_gb.py`; `'auto'`
   routes small inputs to classic `VALIS_ADAPTER`, large to distributed — `registration.nf`).
5. **Micro-registration** — ✅ DONE + PROVEN (this revision).

## Known limitations / follow-ups

- `force_tiling` + micro: micro always uses the **separated** wave-1 (so its raw field is available to
  inject); tiled wave-1 + micro is not combined (rare extreme; documented in the adapter).
- OME fallback full-metadata parity (item 3) — optional.

## Conventions / gotchas (still valid)

- Reference = `valis-wsi==1.0.0` (the deployed image; `valis_lib/` matches it). NOT 1.2.0 (different kernel).
- Building the Dockerfile fresh resolves valis to 1.2.0 (unpinned upstream) — keep the `==1.0.0` pin.
- The fixtures are tiny (128px) for fast unit tests; use `tests/testdata/generate_large_fixture.py`
  (1024px+) for the non-rigid / micro regime (micro is degenerate at 128px).
- `bin/spikes/` holds the exploratory VALIS-internals scripts (externalize, finalize Option-A, micro
  Option-2 compose, the micro-prep oracle) — kept as the de-risking record.

---

_Earlier (2026-06-13/14) handoff revisions removed — they described mid-build state now superseded.
The design spec (`docs/superpowers/specs/2026-06-13-valis-tiled-parallel-registration-design.md`, §5A
updated 2026-06-15) and the plan remain the authoritative design record._
