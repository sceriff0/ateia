# HANDOFF — Low-memory, parallel, VALIS-faithful registration

**Branch:** `feature/reg-lowmem-parallel` (branched from `main` @ `ddd1636`)
**Written:** 2026-07-23
**Spec:** `docs/superpowers/specs/2026-07-22-lowmem-parallel-registration-design.md`
**Plan:** `docs/superpowers/plans/2026-07-22-lowmem-parallel-registration.md`
**Ledger:** `.superpowers/sdd/progress.md` (gitignored; per-task reports alongside it)

## 1. Read this first

The goal: run full-resolution WSIs through registration on a low-resource machine, diverging
from classic VALIS as little as physically possible, working under `mode=add_cycle`, with a
built-in facility to run classic and new registration over the same slides and diff them.

**The core finding the whole branch rests on:** the RAM wall is the slide *reader*, not the
algorithm. `valis.slide_tools.warp_slide()` is `reader.slide2vips()` + `warp_tools.warp_img()`.
The warp is ALREADY pure lazy pyvips; only the read was eager, because
`BioFormatsSlideReader.slide2vips` decodes every tile through a JVM and stitches them,
materializing the whole decompressed slide. VALIS routes multichannel OME-TIFF to BioFormats
(`valis_lib/slide_io.py:2418-2424`) purely as a SPEED heuristic — its own comment says
"very slow for multichannel", not "incorrect". mirage writes its own tiled BigTIFF inputs
(`bin/preprocess.py:391-399`), so a lazy pyvips reader can be injected through VALIS's
supported `Valis.register(reader_cls=...)` hook (`valis_lib/registration.py:3982, 4099`).

**It is a reader swap, never a pixel transformation.** No VALIS algorithm is modified.

## 2. Branch state — 5 implementation commits, all green

```
2b55430  :bug:  Fix reader MetaData contract; make leg 1 exercise the lazy reader
65c90f1  :zap:  Inject the lazy reader into the rigid stages
d0434c9  :zap:  Warp full-res slides through the lazy pyvips reader
22fe6ec  :white_check_mark: Negative regression tests for can_read's six predicates
55e3b2f  :bug:  Tighten can_read gate; dedupe dtype maps; test pyvips writer convention
43991f0  :sparkles: JVM-free lazy pyvips slide reader
7d99428  :bug:  Fail the A1 probe if BioFormats dispatch changes
aceb15f  :white_check_mark: Probe pyvips/BioFormats decode equivalence (spec A1)
b6d2a63 / 3fb20c4 / 9ad0ff7  :memo: plan + spec
```

Working tree clean except pre-existing untracked `illum_bench/` — **keep it untracked, never
`git add -A`** (another agent may share this worktree).

Plan tasks 0-4 COMPLETE (Task 4 added `6850f82` + `59ce171`; see §9). **Tasks 5-8 NOT STARTED.**

## 3. What is actually PROVEN (with the command to reproduce)

| Claim | Evidence |
|---|---|
| pyvips and BioFormats decode mirage's OME-TIFF to identical pixels | `max\|delta\|=0.0`, probe asserts the reader class so it cannot pass vacuously |
| Full-res warp through the lazy reader is **bit-identical** to BioFormats | `LEG 1 ... equal=True max\|delta\|=0.0`, with `can_read=True`, a non-trivial warp (`max\|M-I\|=119.4`, `max\|dxdy\|=11.15`), and BOTH marker lines asserted |
| The reader is genuinely lazy | Only `write_to_memory` is in `slide2image` (contractually required); reviewer-verified |
| Rigid stage runs with `reader_cls` and allocates no slide-scaled heap | `reg_prep exit=0`, prints `no JVM started`, no `AttributeError` |
| `page-height` round-trips through pyvips save/load | new unit tests, both `C>1` and `C==1` |

```bash
docker run --rm -v "$PWD":/work -w /work bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
  python3 tests/unit/test_mirage_slide_reader.py            # expect 11/11 passed
docker run --rm -v "$PWD":/work -w /work bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
  python3 tests/integration/verify_lowmem_bitidentical.py   # expect LEG 1 ... max|delta|=0.0
docker run --rm -v "$PWD":/work -w /work bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
  python3 tests/integration/probe_reader_equivalence.py     # expect A1 VERDICT: PASS
```

## 4. Claims that were CORRECTED — do not repeat these

**4.1 "JVM-free" is WRONG. Say "no slide-scaled JVM heap".**
`Valis.__init__` starts a JVM unconditionally: `registration.py:2083` calls
`slide_tools.get_img_type()`, which calls `slide_io.init_jvm()` — before `reader_cls` is ever
consulted. Spec assumption **A2 is FALSE** and cannot be fixed without touching `valis_lib/`
(forbidden). This does NOT defeat the goal: the RAM wall was BioFormats materializing a slide
into a heap sized `3*filesize+8`; `get_img_type`'s JVM only probes file types at a fixed default
heap. Update the spec's §3/§5.2 wording before merge.

**4.2 The committed test fixtures do NOT resemble production.**
`tests/testdata/P001_*.ome.tiff` are plain `tifffile.imwrite` output — NOT tiled, NOT BigTIFF —
so `can_read()` returns **False** for them. Every integration run before `2b55430` silently fell
back to BioFormats on BOTH sides, making `max|delta|=0.0` real but meaningless with respect to
the reader. `verify_lowmem_bitidentical.py` now rewrites its inputs into genuine tiled BigTIFF
and asserts `can_read()` first. **Any new integration test MUST do the same**, or it will test
the fallback path and look green.
Consider fixing `tests/testdata/generate_complete_testdata.py` to emit tiled BigTIFF — deferred
because other tests depend on the current fixtures.

**4.3 Three of the first five verification gates were initially vacuous.**
All the same shape: they asserted an OUTCOME without pinning the PRECONDITION that makes the
outcome meaningful. (a) the A1 probe could pass without exercising BioFormats; (b) leg 1 could
pass on an identity warp; (c) leg 1 could pass comparing BioFormats to BioFormats. Each is now
guarded. **When adding any comparison test on this branch, assert its preconditions.**

## 5. Remaining work — Tasks 4-8

All have complete, concrete code in the plan file. Start at Task 4.

- **Task 4 — output-tile warp fan-out.** `bin/reg_warp_tile.py`, `bin/reg_assemble.py`,
  `reg_finalize.py --emit-field-only`, `tile_grid.output_grid()`, plus **leg 2** (tile fan-out ==
  single-process warp, `max|delta|=0`). Bit-identical BY CONSTRUCTION: pyvips is demand-driven, so
  `warp(...).crop(x,y,w,h)` computes each output pixel with the same code as the whole-image warp.
  `--rigid-only` must mean `dxdy=None` end-to-end — do NOT substitute a zero field, `warp_img`
  takes different branches for `None` vs a supplied field and equivalence is not established.
  Both new scripts are invoked BY NAME from Nextflow ⇒ `git update-index --chmod=+x`.
- **Task 5 — NF modules + adapter rewiring.** The riskiest task. Splits finalize into
  compose → grid → tile-warp → assemble; DELETES `REG_WARP_REF` (reference goes through the same
  chain with `--rigid-only`, which also removes the 40 GB-heap process that had been OOMing on
  merged references). `nf-test` 0.9.5 IS NOW INSTALLED (see §9).
- **Task 6 — `mode=add_cycle` + `reg_qc=2` fast-fail.**
- **Task 7 — `--reg_compare`** (runs both adapters over the same slides, streams a per-channel diff).
- **Task 8 — REG_PREP stage timings + CI wiring**, including the currently-unreferenced
  `tests/integration/verify_micro_bitidentical.py`.

## 6. Traps that already cost time

- **The VALIS image is linux/amd64 under QEMU on an arm64 host.** `reg_prep.py` takes 5-15 min
  even on 97 KB fixtures. Run docker in the FOREGROUND, once. Three subagents failed by
  backgrounding it and polling, concluding "finished" when it hadn't. A running container has
  not failed. Also: kill stray containers (`docker ps`) — one ran 17 h unnoticed.
- **Subagents cannot block across a turn boundary.** For container work, have the subagent write
  code and let the CONTROLLER run the verification.
- **Do not trust agent-reported log lines.** A Task 2 report quoted a `skipping JVM` line that
  could not have been printed given the fixtures. Reproduce runtime claims yourself.
- **`pytest` is NOT in the VALIS image; `pyvips`/`valis` are NOT on the host.** Unit tests need
  the `try: import pytest / except ImportError:` guard AND a stdlib `__main__` runner — copy
  `tests/unit/test_tile_grid.py`'s structure. A bare `pytest.importorskip` SKIPS on the host and
  cannot run in-image, so it verifies nothing anywhere.
- **Nextflow facts established experimentally (25.04.7):** `withName` selectors match as a
  SUBSTRING FIND, not a full match (`REG_NONRIGID` also matches `REG_NONRIGID_MICRO`).
  `path(x)` with no `arity` accepts `[]` and renders empty (this is how the optional field works);
  `arity: '0..1'` combined with `stageAs` is REJECTED.
- **`valis_lib/` is a pristine read-only copy of PyPI valis-wsi 1.0.0.** Never modify it; the
  faithfulness argument depends on it.

## 7. Known open items (none blocking)

1. **RESOLVED 2026-07-23 — benign, no action needed.** `reg_prep` exits 0 but prints a
   `Traceback`. It is NOT the `TilesPending` halt (that was the wrong guess). It is VALIS's own
   registrar-pickle step failing:
   `registration.py:4144 pickle.dump(self, ...)` ->
   `ffi.error: struct _VipsObject: wrong size for field 'constructed' (cdef says 1, but C
   compiler says 4)` — a pyvips cffi/libvips ABI mismatch, caught by `register()`'s broad
   `except` and logged.
   Harmless HERE **by design**: pyvips images are unpicklable, which is why the distributed path
   was built on a plain-data handoff (`.v`/`.npy`/JSON) and never consumes that pickle.
   Confirmed `reg_prep` still does its real work — dumps both warp states plus `tiler_inputs/`,
   and `warp_state.json` carries all 12 expected keys (`M`, `aligned_slide_shape_rc`,
   `bbox_xywh`, `bg_color`, `from_rigid_reg`, `internal_pad`, `interp_method`, `is_rgb`,
   `processed_img_shape_rc`, `reg_img_shape_rc`, `series`, `slide_name`).
   ⚠️ SEPARATE CONCERN, unrelated to this branch: the CLASSIC path DOES need that pickle
   (`reg_qc=2` warps GeoJSON polygons through it via `WARP_SEG_QC`). If this ABI mismatch also
   occurs on the cluster, classic `reg_qc=2` is broken there too. Most likely local to this
   image/QEMU emulation — worth a one-off check on real infrastructure.
2. `conf/modules.config:260` sets only `publishDir` for `REG_NONRIGID|REG_MICRO_PREP`, so
   `REG_NONRIGID` inherits `process_medium` = **200 GB** for single-digit-GB work; `REG_MICRO_PREP`
   inherits `process_high` = 300 GB. Task 5 fixes this.
3. Pre-existing JVM leak: `bin/reg_finalize.py:215` `raise SystemExit` sits after the JVM-start
   block. Confirmed NOT introduced by this branch.
4. `can_read()` accepts a non-OME grayscale multi-page tiled BigTIFF (pyvips auto-populates
   `page-height`). Reviewer judged acceptable: pixels stay correct, only channel NAMING degrades to
   `C0/C1`. **But** `workflows/mirage.nf:213` shows `--start registration` takes USER-SUPPLIED
   paths, so a user file CAN reach the reader. Final review should confirm nothing downstream
   depends on reader-derived `channel_names` rather than `CONVERT_IMAGE`'s `*_channels.txt`.
5. `warp_source` docstring should note it assumes `ws["M"]`/`ws["series"]` are always populated.

## 8. Immediate next action

```bash
git checkout feature/reg-lowmem-parallel
cat .superpowers/sdd/progress.md          # per-task history + review findings
```

**Tasks 0-4 are COMPLETE.** Start Task 5 from the plan — but first run the review pass that is
still owed over `65c90f1..HEAD` (Task 3 was never formally reviewed and four commits now sit on
it, and Task 5 rewires exactly that code).

Execution originally ran via `superpowers:subagent-driven-development`, one subagent per task plus
a task review; `.superpowers/sdd/` holds every brief, report and review diff. Task 4 was executed
directly by the controller instead (no subagents), which is why it has a self-review rather than
an independent one.

## 9. Task 4 — done, and what it changed about the picture

Commits: `6850f82` (tile fan-out), `59ce171` (compress the tiles).

| Claim | Evidence |
|---|---|
| Tile fan-out == single-process warp | `LEG 2 ... equal=True max\|delta\|=0.0`, 9 tiles in a 3x3 grid, 5 of them ragged-edged |
| Compressed tiles are lossless end-to-end | same leg 2, with `.tif` deflate+predictor tiles; round-trip also checked directly for every tile shape leg 2 emits x uchar/ushort/float |
| `output_grid` partitions exactly | unit tests paint a coverage counter — catches gaps AND overlaps; `9/9 passed` |

**`nf-test` install: `brew install nf-test` DOES NOT EXIST** (no such formula — this handoff said
otherwise and it cost a cycle). Installed instead from the pinned GitHub release: download
`nf-test-0.9.5.tar.gz` from `askimed/nf-test` releases, put `nf-test.jar` in `~/.nf-test/` and the
`nf-test` wrapper on PATH. `nf-test version` -> 0.9.5, Nextflow 25.04.7. Avoid `nf-test update`,
which curl-pipes to bash. Bioconda (`conda install -c bioconda nf-test`) also works.

**A vacuity gap was found and closed in the EXISTING leg 1.** `px()` read with pyvips' default
`n=1`, and BOTH writers in play stack channels vertically into pages (valis
`slide_io.save_ome_tiff` does `arrayjoin(bandsplit(), across=1)`; so does `_save_ome_pyvips`) — so
every `max|delta|=0.0` on this branch before `6850f82` covered **channel 0 of 3**. Now reads
`n=-1` and asserts the loaded array is the full C x H x W stack. Same family as §4.3; assume more
of them exist.

**New traps, both real:**
- The output canvas is NOT `aligned_slide_shape_rc` — `warp_img` crops to `bbox_xywh`
  (`warp_tools.py:938-943`). Ask the lazy warp for `.width`/`.height` instead of predicting it;
  that costs nothing because pyvips knows dimensions without decoding.
- TIFF tile dimensions must be multiples of 16 and cannot exceed the image. A fixed 512px internal
  tile size made small tiles unreadable (`tile size out of range`) — invisible at production tile
  sizes (4096px), caught only because leg 2 uses a deliberately awkward 48px ragged grid. **Keep
  new tests biased toward awkward geometry, not round numbers.**

**Test ergonomics:** `VERIFY_LOWMEM_WORK=<dir>` + `VERIFY_LOWMEM_REUSE_PREP=1` skips the 5-15 min
`reg_prep` and reuses an existing prep (opt-in, loudly announced, off in CI). Point `WORK` at a
bind mount so it survives `docker run --rm`. Cuts the iteration cycle from ~12 min to ~3.

## 10. Open items carried into Task 5

1. **Review pass owed over `65c90f1..HEAD`** — see §8. Highest priority.
2. **No provenance binds a tile to the field/warp state that produced it.** `_open_tile` checks
   dimensions only, so a stale same-sized tile assembles silently. Not reachable today; Task 5's
   `-resume` and cached task dirs are exactly what make it reachable. Design it in, don't retrofit:
   stamp `grid.json` with a hash of field + warp state and check it at assembly.
3. **Tile scratch is still large even compressed.** Measured 1.43x on 128px synthetic fixtures,
   which is NOT a capacity-planning number (real slides have far more spatial correlation). Budget
   disk in the Task 5 process directives.
4. `reg_assemble` opens every tile before consuming any — fine at the 4096px default (~600 tiles
   for a whole slide), but ~38k open images at `--tile-wh 512`. Document a floor.
5. Items 2-5 of §7 (modules.config memory labels, the reg_finalize JVM-leak line, can_read
   accepting non-OME grayscale, the warp_source docstring) are all still open.
