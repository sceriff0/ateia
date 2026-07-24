# HANDOFF — Low-memory, parallel, VALIS-faithful registration

**Branch:** `feature/reg-lowmem-parallel` (from `main` @ `ddd1636`) · **HEAD:** `0c1656d`
**Rewritten:** 2026-07-23 (supersedes the layered version; this is the single restart point)
**Spec:** `docs/superpowers/specs/2026-07-22-lowmem-parallel-registration-design.md`
**Plan:** `docs/superpowers/plans/2026-07-22-lowmem-parallel-registration.md`
**Ledger:** `.superpowers/sdd/progress.md` (gitignored; per-task briefs/reports alongside)

---

## 1. Read this first

Goal: run full-resolution WSIs through registration on a low-resource machine, diverging from
classic VALIS as little as physically possible, working under `mode=add_cycle`, with a facility to
run classic and new registration over the same slides and diff them.

**The finding the whole branch rests on:** the RAM wall is the slide *reader*, not the algorithm.
`valis.slide_tools.warp_slide()` is `reader.slide2vips()` + `warp_tools.warp_img()`. The warp was
ALREADY pure lazy pyvips; only the read was eager, because `BioFormatsSlideReader.slide2vips`
decodes every tile through a JVM and stitches them, materializing the whole decompressed slide.
VALIS routes multichannel OME-TIFF to BioFormats (`valis_lib/slide_io.py:2418-2424`) purely as a
SPEED heuristic — its own comment says "very slow for multichannel", not "incorrect". mirage writes
its own tiled BigTIFF inputs (`bin/preprocess.py:391-399`), so a lazy pyvips reader is injected
through VALIS's supported `Valis.register(reader_cls=...)` hook.

**It is a reader swap plus a fan-out, never a pixel transformation.** No VALIS algorithm is
modified. `valis_lib/` is a pristine read-only copy of PyPI valis-wsi 1.0.0 — never modify it; the
faithfulness argument depends on that.

---

## 2. State: ALL TASKS 0-8 COMPLETE — the branch is now in review/merge territory

The plan is finished. What remains is not implementation but the open items in §7 — chiefly that
**nothing has ever run on a real slide** (§7.2), which is the branch's entire purpose.

```
3be3da9 :wrench:           REG_PREP stage timings + bit-identity checks in CI       <- task 8
d03ff54 :sparkles:         --reg_compare: run both paths, diff them                 <- task 7
70a3d41 :sparkles:         add_cycle on the distributed path + reg_qc=2 fast-fail   <- task 6
0c1656d :white_check_mark: Tag the new reg tests stub so CI actually runs them
ff25787 :recycle:          Split finalize into compose + grid + tile-warp + assemble   <- task 5
6d7ad64 :bug:              Fix review findings: false JVM log line + 2 unrunnable tests
59ce171 :zap:              Compress the intermediate warp tiles                        <- task 4
6850f82 :sparkles:         Split the full-res warp into independent output tiles       <- task 4
2b55430 / 65c90f1          lazy reader in the rigid stages                             <- task 3
d0434c9                    full-res warp through the lazy reader                       <- task 2
43991f0..22fe6ec           the lazy reader itself                                      <- task 1
aceb15f / 7d99428          A1 probe: pyvips == BioFormats                              <- task 0
```

Working tree clean except pre-existing untracked `illum_bench/` — **keep it untracked, never
`git add -A`** (another agent may share this worktree; re-check HEAD before every git write).

The pipeline shape after task 5:

```
REG_PREP -> REG_TILE(fan-out) -> REG_COMPOSE_{TILED,FIELD,MICRO} -> slide_dxdy.v
         -> REG_GRID -> REG_WARP_TILE (fan-out, one task per OUTPUT tile) -> REG_ASSEMBLE
```

`REG_WARP_REF` is DELETED — the reference flows through the same chain with `--rigid-only`, which
also removed the 40 GB-heap process that had been OOMing on merged multi-cycle references.

---

## 3. What is PROVEN, and how to reproduce it

| Claim | Evidence |
|---|---|
| pyvips and BioFormats decode mirage's OME-TIFF to identical pixels | `max\|delta\|=0.0`; probe asserts the reader class so it cannot pass vacuously |
| Full-res warp through the lazy reader == BioFormats | `LEG 1 equal=True max\|delta\|=0.0`, all 3 channels, both reader markers asserted, non-trivial warp (`max\|M-I\|=119.4`, `max\|dxdy\|=11.15`) |
| Tile fan-out == single-process warp | `LEG 2 equal=True max\|delta\|=0.0`, 9 tiles in a 3x3 grid, 5 ragged-edged |
| Compressed tiles are lossless end-to-end | same leg 2 with deflate+predictor `.tif` tiles; round-trip also checked directly for every tile shape leg 2 emits x uchar/ushort/float |
| Distributed tiled registration == in-process VALIS tiler | `DISTRIBUTED TILED REGISTRATION BIT-IDENTICAL: True` |
| `output_grid` partitions exactly (no gaps/overlaps) | unit tests paint a coverage counter; `9/9 passed` |
| NF wiring works and the default path is untouched | 6/6 nf-tests on the CI gate; full stub run completes, still routes to `VALIS_ADAPTER:REGISTER`, no fan-out tasks |
| `--reg_compare` runs BOTH paths and diffs them | paired nf-tests assert WHICH processes ran (`REGISTER` + `REG_PREP` + `REG_ASSEMBLE` + `COMPARE_REGISTRATION` present with the flag, `COMPARE_REGISTRATION`/`REG_PREP` absent without it); full stub run `EXIT 0`, no config-selector WARNs, outputs in 3 distinct dirs |
| the diff tool measures what it claims | `test_compare_registration.py` 6/6 in-image: every "they agree" paired with a perturbation of known exact magnitude; a LAST-channel-only perturbation (catches a regression to pyvips' default `n=1`); ragged tiled sweep == single-shot; comparing a file with itself fails loudly |
| moving the band-join into `vips_pages` changed no reader behaviour | `test_mirage_slide_reader.py` **11/11** in-image after the move (same 11 as before it) |
| restructuring `reg_prep` for stage timings changed no pixels | `verify_lowmem_bitidentical.py` re-run FROM SCRATCH at `3be3da9` (that harness runs `bin/reg_prep.py`): both legs `equal=True max\|delta\|=0.0` |

```bash
# unit (in-image)
docker run --rm -v "$PWD":/work -w /work bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
  python3 tests/unit/test_tile_grid.py                       # 9/9
docker run --rm -v "$PWD":/work -w /work bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
  python3 tests/unit/test_mirage_slide_reader.py             # 11/11

# integration (in-image) — legs 1 and 2
docker run --rm -v "$PWD":/work -w /work bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
  python3 tests/integration/verify_lowmem_bitidentical.py
docker run --rm -v "$PWD":/work -w /work bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
  python3 tests/integration/verify_distributed_bitidentical.py

# nextflow — run the generator FIRST (see §5.2)
python tests/testdata/generate_complete_testdata.py
nf-test test --tag stub --profile test                        # the actual CI gate
nextflow run . -profile test,docker -stub --outdir /tmp/stubout
```

**Provenance caveat — RESOLVED.** `59ce171`'s leg 2 had only ever run on REUSED prep
(`VERIFY_LOWMEM_REUSE_PREP=1`). Task 8 re-ran `verify_lowmem_bitidentical.py` from scratch (no
reuse) at `3be3da9` to prove the restructured `reg_prep.py` unchanged, and it came back
`LEG 1 equal=True max|delta|=0.0` / `LEG 2 equal=True max|delta|=0.0` with both reader markers and
full 3-channel coverage asserted. Leg 2 now has clean-run provenance.

---

## 4. Claims that were CORRECTED — do not repeat these

**4.1 "JVM-free" is WRONG. Say "no slide-scaled JVM heap."** `Valis.__init__` starts a JVM
unconditionally: `registration.py:2083` -> `slide_tools.get_img_type()` -> `slide_io.init_jvm()`
when `BF_READABLE_FORMATS is None`, before `reader_cls` is consulted. Spec assumption **A2 is
FALSE** and cannot be fixed without touching `valis_lib/` (forbidden). It does NOT defeat the goal:
that JVM only sniffs file extensions at a default heap, whereas the wall was BioFormats decoding a
slide into a heap sized `3*filesize+8`. **The spec's §3/§5.2 wording still needs updating before
merge.** The prep scripts' log lines were fixed in `6d7ad64`.

**4.2 `withName` is a FULL regex match, NOT a substring find.** This handoff previously claimed the
opposite and task 5 was built on it. Measured on 25.04.7: `withName: 'REG_COMPOSE'` matched NONE of
`REG_COMPOSE_TILED`/`_FIELD`/`_MICRO`. Nextflow reports it ONLY as
`WARN: There's no process matching config selector` — never an error — after which the processes
silently inherit their module label AND the global default publishDir, so every patient's output
collides in one directory. **Read the stub run's WARNings, not just its exit status.** Alternate
names explicitly: `'A|B|C'`. (`REG_NONRIGID` matching `REG_NONRIGID_MICRO` is explained by the
alias `include { REG_NONRIGID as REG_NONRIGID_MICRO }`, not by substring matching.)

**4.3 The committed fixtures do NOT resemble production.** `tests/testdata/P001_*.ome.tiff` are
plain `tifffile.imwrite` output — NOT tiled, NOT BigTIFF — so `can_read()` returns **False** for
them. `verify_lowmem_bitidentical.py` rewrites its inputs into genuine tiled BigTIFF and asserts
`can_read()` first. **Any new integration test MUST do the same**, or it silently tests the
BioFormats fallback and looks green.

**4.4 VACUITY IS ENDEMIC ON THIS BRANCH — assume it until disproven.** Five instances so far, all
the same shape: assert an OUTCOME without pinning the PRECONDITION that makes it meaningful.
(a) the A1 probe could pass without exercising BioFormats; (b) leg 1 could pass on an identity
warp; (c) leg 1 could pass comparing BioFormats to BioFormats; (d) **`px()` read with pyvips'
default `n=1`, so every `max|delta|=0.0` before `6850f82` compared CHANNEL 0 OF 3** — both writers
stack channels as vertically-stacked PAGES (`slide_io.save_ome_tiff` does
`arrayjoin(bandsplit(), across=1)`; `_save_ome_pyvips` likewise); (e) the same `n=1` bug made
`verify_distributed_bitidentical.py` and `compare_classic_vs_distributed.py` **unable to pass at
all** — they diffed a page-0 read against an in-memory `(H,W,C)` array. Note `n=-1` alone does NOT
fix it (that gives `(C*H, W)`); the band-join is load-bearing, hence
`mirage_slide_reader.open_multiband()`. **When adding any comparison test, assert its
preconditions.**

---

## 5. Traps that already cost time

### 5.1 Container work
- **The VALIS image is linux/amd64 under QEMU on an arm64 host.** `reg_prep.py` takes 5-15 min even
  on 97 KB fixtures. Run docker ONCE, and either foreground it or use a harness-tracked background
  job — do NOT poll and conclude "finished". Kill strays (`docker ps`); one ran 17 h unnoticed.
- **Docker on this host WEDGES, and the symptom looks like a slow test.** Containers are created
  and never start: `docker ps` shows nothing, `docker ps -a` shows `Created` forever, the client
  process hangs, and `docker info` still answers normally — so the daemon looks healthy. Seen in
  task 7 on both a `docker run` and a `-profile test,docker -stub` pipeline run (which stalled
  mid-DAG at `SPLIT_CHANNELS`). **Check `docker ps -a` for `Created` before believing a container
  is merely slow**, then `pkill -f "docker run"; docker ps -aq | xargs docker rm -f` and restart
  Docker Desktop. Do not read a stalled run as a failure of whatever you just changed.
- **The repo is bind-mounted into the container.** Editing files while a run is in flight changes
  what is being verified. Wait, or edit only files the run does not read.
- **`pytest` is NOT in the VALIS image; `pyvips`/`valis` are NOT on the host.** Unit tests need the
  `try: import pytest / except ImportError:` guard AND a stdlib `__main__` runner — copy
  `tests/unit/test_tile_grid.py`. A bare `pytest.importorskip` SKIPS on the host and cannot run
  in-image, so it verifies nothing anywhere.
- **Iterate fast:** `VERIFY_LOWMEM_WORK=<dir>` + `VERIFY_LOWMEM_REUSE_PREP=1` skips `reg_prep`
  (~12 min -> ~3 min). Opt-in, loudly announced, off in CI. Point `WORK` at a bind mount so it
  survives `--rm`, and do not use it for an authoritative result.

### 5.2 Running nf-test locally — two traps that make an untouched tree look broken
1. **Run `python tests/testdata/generate_complete_testdata.py` FIRST.** 11 committed CSVs (incl.
   `test_input.csv`) hardcode absolute paths to `/Users/valer/Downloads/mirage` — a different
   checkout. The generator rewrites them from `Path(__file__).parent`; CI runs it, so CI is fine.
   Without it, 16 pipeline/subworkflow tests fail with "Input file does not exist" and look like a
   regression in whatever you just changed. The generator also REWRITES `tests/testdata/*.ome.tiff`,
   which the bit-identity tests consume — check `git status` afterwards.
2. **Use the CI gate's own command: `nf-test test --tag stub --profile test`** (no docker).
   `--profile test,docker` makes even stub blocks execute inside their container, so every process
   whose image is not pulled locally fails with exit 125 — 15 spurious failures. Also `tag "stub"`
   goes INSIDE each test block next to `options "-stub"`; a test without it is silently SKIPPED by
   the CI gate. **The two are independent and you need BOTH**: `tag "stub"` is only a CI label, so
   a test carrying the tag but missing `options "-stub"` EXECUTES FOR REAL and dies on
   `ModuleNotFoundError: No module named 'valis'`. (Validation tests that fail at launch get away
   without it — nothing executes — which is why the existing ones look like counter-examples.)
3. **`workflow.trace.tasks()` returns `WorkflowTask`, which has exactly two properties: `name` and
   `success`.** There is NO `.process`. Derive the process name from `name`: strip the trailing
   ` (tag)`, take the last `:`-separated segment, then compare EXACTLY — substring matching walks
   straight into the §4.2 trap (`REG_COMPOSE` "matches" all three `REG_COMPOSE_*`).
4. **Assert failure REASONS, not `workflow.failed`.** A launch-time error message lands in
   `workflow.stdout` (a list of lines), not `workflow.errorReport`, which is `''`.

### 5.3 Geometry and formats
- **The output canvas is NOT `aligned_slide_shape_rc`** — `warp_img` crops to `bbox_xywh`
  (`warp_tools.py:938-943`). Ask the lazy warp for `.width`/`.height` instead of predicting it;
  free, because pyvips knows dimensions without decoding.
- **TIFF tile dimensions must be multiples of 16 and cannot exceed the image.** A fixed 512px
  internal tile size made small tiles unreadable (`tile size out of range`) — invisible at
  production tile sizes (4096px), caught only by leg 2's deliberately awkward 48px ragged grid.
  **Keep test geometry awkward, not round.**
- **`--rigid-only` must mean `dxdy=None` end-to-end** — do NOT substitute a zero field; `warp_img`
  takes different branches for `None` vs a supplied field and equivalence is not established.
- Scripts invoked BY NAME from Nextflow must be git-mode `100755` (`git update-index --chmod=+x`).

---

## 6. What the last three tasks landed

All plan tasks are done. Kept here because each carries a decision the code alone does not explain.

- **Task 6 — DONE** (`70a3d41`). `ParamUtils.useDistributedAdapter` / `regQcLevel` /
  `validateRegistrationPath`; add_cycle now takes the same adapter as a full run; `reg_qc=2` +
  distributed fails at launch from both call sites; 4 new nf-tests on the CI `stub` tag.
  One deliberate divergence: add_cycle does NOT replicate `reg_dist_sub_threshold='auto'`'s
  per-patient REG_ESTIMATE routing — with the switch on it always goes distributed. Harmless
  under the default `reg_dist_force_tiling=false`. The Task-7 note said to fold a shared adapter
  selector in — **not done, and on reflection it is orthogonal**: `REG_COMPARE` invokes both
  adapters *unconditionally*, so it never exercises the selector. Still worth doing; now a
  standalone item (§7.12).
- **Task 7 — DONE** (`d03ff54`). `--reg_compare` runs classic AND the new path over the same
  slides and streams a per-channel diff; classic stays the run's real output. New:
  `bin/compare_registration.py` (100755), `bin/utils/vips_pages.py`,
  `modules/local/compare_registration.nf`, `subworkflows/local/reg_compare.nf`,
  `tests/unit/test_compare_registration.py` (6/6 in-image), 2 paired nf-tests on the CI `stub`
  tag. `ParamUtils.boolParam` / `regCompareEnabled`; `validateRegistrationPath` returns early
  under compare (classic runs, so `reg_qc=2` keeps its pickle).

  Outputs land in three distinct directories, on purpose:
  `registered/registered_slides/` (classic = the run's output, and what the checkpoint CSV
  lists), `registered/candidate/registered_slides/` (the low-memory path),
  `registered/compare/<patient>_<channels>_regcompare.json` + `_regdiff.png`.

  The plan file's §Task 7 lists all six divergences from its own code and why each was forced.
  The one to carry forward: **the plan's `[patient_id, meta.id]` join key would have produced
  ZERO comparisons, silently** — `meta.id` is absent on registration metas, so every slide of a
  patient collapses onto `[pid, null]`, and `join` drops unmatched keys without a word. The
  shipped key is `[patient_id, sorted channel signature]` with
  `failOnMismatch`/`failOnDuplicate`. Sixth instance of the §4.4 shape.
- **Task 8 — DONE** (`3be3da9`). The gap it closed: **no bit-identity guarantee on this branch ran
  automatically.** Two CI jobs now, split by cost:

  | Job | Trigger | Blocking? | Runs |
  |---|---|---|---|
  | `valis-unit` | every push/PR | **yes** — in the `all-tests` gate | `test_tile_grid`, `test_mirage_slide_reader`, `test_compare_registration` (image needed for pyvips/valis, but no registration run) |
  | `distributed-integration` | push to main/dev **+** dispatch (was dispatch-only) | no | `verify_distributed_bitidentical`, `probe_reader_equivalence`, `verify_lowmem_bitidentical`, `verify_micro_bitidentical` (a real `reg_prep`, too slow per-PR) |

  `verify_micro_bitidentical.py` had existed in the repo referenced by **no workflow at all**.

  `REG_PREP` also writes per-phase wall-clock (`load` / `rigid_and_prep` / `dump`) to
  `<outdir>/<patient>/registered/timings/<patient>_reg_prep_timings.json`. It is the one process
  on this path still doing whole-slide work, so its cost is the one thing the DAG does not show.

  **Trap found doing it:** declaring the timings output nested as `prep/stage_timings.json` looked
  right, ran green, and published an **empty** `timings/` directory. `publishDir`'s `pattern`
  cannot reach inside a directory output — `prep/` publishes as a unit or not at all. The file is
  lifted to the task root like `*.size.csv`. **Check the published tree, not the exit status.**

  Still true and NOT ours: the full gate (`nf-test test --tag stub --profile test`) is
  **113/114**; the single failure is `tests/modules/generate_qc_report.nf.test` — *"declares 7
  input channels but 6 were specified"*. `db37397` **on main** added a 7th input (`seg_eval_csvs`)
  without updating the test. A green gate needs it fixed.

---

## 7. Open items

**Blocking merge**
1. **Spec §3/§5.2 wording** still says JVM-free (see §4.1).
2. **Nothing has ever run on a real slide. This is now the ONLY implementation-shaped thing left,
   and it is the branch's entire purpose.** All evidence is on 128px synthetic fixtures.
   Bit-identity is structural so it should hold, but peak RSS, disk footprint, tile counts,
   wall-clock and compression ratio on a real WSI are entirely UNMEASURED. The 1.43x compression
   measured on fixtures is NOT a capacity-planning number.

   Tasks 7 and 8 built the two instruments for exactly this and neither has been pointed at real
   data yet. The intended first run:
   ```bash
   nextflow run . -profile <cluster> --reg_distributed_tiling true --reg_compare true \
       --reg_mem_budget_gb <machine RAM> --input <real cohort>.csv --outdir <out>
   ```
   then read `<out>/<patient>/registered/compare/*_regcompare.json` (how far the paths diverge,
   per channel) against `<out>/<patient>/registered/timings/*_reg_prep_timings.json` and the
   Nextflow trace (where the time and memory actually went). Budget ~2x registration for the
   comparison; drop `--reg_compare` once the numbers exist.

**Should decide before merge**
3. **`can_read()` accepts a non-OME grayscale multi-page tiled BigTIFF** (pyvips auto-populates
   `page-height`), degrading channel names to `C0/C1`. `workflows/mirage.nf:213` shows
   `--start registration` takes USER-SUPPLIED paths, so this is reachable. Channel names feed the
   case-sensitive downstream measurement-key contract — **confirm nothing downstream depends on
   reader-derived `channel_names` rather than `CONVERT_IMAGE`'s `*_channels.txt`.**
4. **Committed CSVs hardcode `/Users/valer/Downloads/mirage`** (§5.2). Makes the local suite
   unusable for anyone else and is likely part of why CI has been red. Clean fix: gitignore them as
   generated artifacts, or make the paths relative. Repo-hygiene call, deliberately not made
   unilaterally.
5. **Tile scratch is still large even compressed.** Budget disk in the Task 5 process directives.
6. `reg_assemble` opens every tile before consuming any — fine at the 4096px default (~600 tiles),
   but ~38k open images at `--tile-wh 512`. Document a floor.

**Low priority, recorded**
7. Nothing binds a tile to the field/warp state that produced it (`_open_tile` checks dimensions
   only). Largely handled by Nextflow's resume cache hashing input file contents; a real hazard
   only for manual/local runs.
8. `kill_jvm()` is skipped when `heap==0` even though a default-heap JVM is running, so it lives
   until process exit (bounded, batch).
9. Unreachable `if md.is_rgb:` branch in `_build_metadata` (`is_rgb` is hardcoded False above it).
10. `warp_source` docstring should note it assumes `ws["M"]`/`ws["series"]` are always populated.
11. The pyvips/libvips cffi ABI mismatch that breaks VALIS's registrar pickle is benign here by
    design, **but classic `reg_qc=2` NEEDS that pickle** (`WARP_SEG_QC` warps GeoJSON through it).
    If it reproduces on the cluster, classic `reg_qc=2` is already broken there. Unverified on real
    infrastructure — worth a one-off check.
12. **The adapter selector is still duplicated** between `registration.nf` (3-way: classic /
    force-distributed / `auto` + REG_ESTIMATE per-patient routing) and `add_cycle.nf` (2-way, no
    `auto`). Task 6's note assigned the fold to Task 7; Task 7 turned out not to need it
    (`REG_COMPARE` invokes both adapters unconditionally). Doing it means one subworkflow both
    call — check first that `REGISTRATION` and `ADD_CYCLE` can never both be invoked in one run,
    since a DSL2 workflow cannot be invoked twice without aliasing.
13. `--reg_compare` reports differences but never fails on them: there is no tolerance gate. That
    is deliberate for a measurement tool, but once a real-slide number exists, a
    `--reg_compare_max_abs` that turns the comparison into a CI assertion is the obvious follow-up.

---

## 8. Immediate next action

The plan is finished; there is no next task to start. In priority order:

```bash
git checkout feature/reg-lowmem-parallel     # HEAD should be 3be3da9
cat .superpowers/sdd/progress.md             # per-task history + review findings
python tests/testdata/generate_complete_testdata.py   # BEFORE any nf-test
docker ps -a                                 # 'Created' forever = wedged, see §5.1
```

1. **Run it on a real slide** (§7.2) — the command and what to read are written out there. Nothing
   else on this list changes what we know about whether the branch achieves its purpose.
2. **Push the branch and watch CI.** The new `valis-unit` job is blocking and has never executed on
   a runner; it is written against the same image the local runs used, but "works locally" is not
   "works on ubuntu-latest".
3. **Fix `generate_qc_report.nf.test`** (§6) — not ours, but it is the last thing between this
   branch and a green gate.
4. **Fix the spec's JVM-free wording** (§7.1) — the only item explicitly marked blocking-merge that
   is purely editorial.

Tasks 0-5 have all been reviewed: the review over `d0434c9..HEAD` was completed 2026-07-23
(verdict PASS, 0 critical) and its findings are fixed in `6d7ad64`. Tasks 6 (`70a3d41`), 7
(`d03ff54`) and 8 (`3be3da9`) carry a controller self-review only — **an independent pass over
`6850f82..HEAD` with the §4.4 lens is the single highest-value review left**, given that the
vacuity pattern has now recurred six times, most recently in task 7's join key.

**Tooling note:** `nf-test` 0.9.5 is installed (jar in `~/.nf-test/`, wrapper on PATH).
`brew install nf-test` DOES NOT EXIST — install from the pinned `askimed/nf-test` GitHub release,
or bioconda. Avoid `nf-test update`, which curl-pipes to bash.

**Process note:** Tasks 0-3 ran via `superpowers:subagent-driven-development`, one subagent per
task plus a task review. Tasks 4-5 were executed directly by the controller with no subagents, so
they carry a controller self-review rather than an independent one. If subagents are available
again, an independent pass over `6850f82..HEAD` would be worth having — with the §4.4 lens.
