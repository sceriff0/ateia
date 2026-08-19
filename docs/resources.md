# Resources

<p class="standfirst">What every process actually asks the scheduler for, where that number comes from,
how it grows on a retry, and what clamps it. Read this before sizing a cluster allocation or
diagnosing an out-of-memory kill.</p>

!!! abstract "Canonical sources"
    - **Per-process requests** — `conf/modules.config` (`withLabel:` and `withName:` blocks)
    - **Fallback defaults & retry policy** — `conf/base.config`
    - **Global ceilings** — `nextflow.config` (`params.max_*`, `process.resourceLimits`)

---

## The one-owner rule

A process's `cpus` / `memory` / `time` come from **either** a resource `label`
**or** a `withName:` block in `conf/modules.config` — never both.

`withName:` wins over `withLabel:`, so a label on a fully-overridden process is
inert and misleading; those have been removed. What remains is three cases:

<div class="gate">
  <div class="g"><div class="k">case 1</div><div class="v">withName owns all three</div><div class="d">No label. The block sets cpus, memory and time. 15 processes.</div></div>
  <div class="g"><div class="k">case 2</div><div class="v">label owns all three</div><div class="d">The withName block, if any, sets only publishDir / ext.args. 6 processes.</div></div>
  <div class="g"><div class="k">case 3</div><div class="v">partial override</div><div class="d">withName sets one or two fields; a label supplies the rest. 6 processes.</div></div>
</div>

**These three counts cover `modules/local/*.nf` only**, because that is what
`tests/test_resource_label_coverage.py` scans and the counts are checked against
that scan — raising one to include a process outside `modules/local/` makes the
build fail (verified: bumping case 3 to `7` fails with `claims [14, 6, 7] … give
[14, 6, 6]`). One process is therefore outside all three numbers: **`BASICPY`**,
in `modules/nf-core/basicpy/`, which is a **case-3 partial override** — upstream's
`label 'process_single'` with `memory` raised to `32 GB × attempt` by its
`withName:` block. Nothing guards that sentence, so check it by hand if you edit
either side.

Case 3 is the one that surprises people: `TILED_SOLVE` carries
`process_single` **and** a `withName:` block, but that block sets `memory`
only — so the label still owns its `cpus` and `time`. All four tiled/STARE
processes work this way, and so do `GENERATE_REGISTRATION_QC` (`withName` sets
`cpus` and `memory`; the `process_high` label still owns `time`) and
`EXPORT_SPATIALDATA` (`withName` sets `time` alone).

`EXPORT_SPATIALDATA` is also the one process in the repo whose `label` is an
*expression* rather than a literal —
`params.spatialdata_include_image ? 'process_high' : 'process_medium'`
(`modules/local/export_spatialdata.nf:22`) — so its `cpus` and `memory` follow
that flag.

---

## Resource labels

The four labels defined in `conf/modules.config`. Every value scales with
`task.attempt`, so attempt 2 is the second column, attempt 3 the third, up to
`maxRetries = 3`.

| Label | `cpus` | `memory` | `time` |
|---|---|---|---|
| `process_single` | `1` | `12.GB × attempt` | `8.h × attempt` |
| `process_low` | `2` | `32.GB × attempt` | `2.h × attempt` |
| `process_medium` | `4` | `100.GB + 100.GB × attempt` | `4.h × attempt` |
| `process_high` | `8` | `200.GB + 100.GB × attempt` | `12.h × attempt` |

Note the `+` in `process_medium` and `process_high`: the first attempt already
gets 200 GB and 300 GB respectively, and each retry adds 100 GB rather than
doubling.

### Fallback defaults

A process with neither a label nor a `withName:` field for a given resource
falls through to `conf/base.config`:

| Resource | Default |
|---|---|
| `cpus` | `1 × attempt` |
| `memory` | `6.GB × attempt` |
| `time` | `4.h × attempt` |

**No process currently relies on this.** Every process has a `label` or a
`withName:` field covering each of `cpus` / `memory` / `time`, and
`tests/test_resource_label_coverage.py` fails the build if one does not. The
table above is what a *new* process would silently get if it were added with
neither — which is why that guard exists.

---

## Per-process requests

Effective values on **attempt 1**. `f` denotes the relevant input size in GiB
(rounded down, minimum 1).

### Preprocessing

| Process | `cpus` | `memory` (attempt 1) | `time` | Owner |
|---|---|---|---|---|
| `CONVERT_IMAGE` | `1` | `24 GB` + tier: `f<4` → +0, `f<12` → +24, `f<24` → +48, else +64 GB | `2.h × attempt` | `withName` |
| `TILE_FOR_BASIC` | `2` | derived from `preproc_tile_size`, `× attempt` *(withName)* — floor 6 GB | `2.h × attempt` | `withName` |
| `APPLY_PROFILES` | `2` | derived from `preproc_tile_size`, `× attempt` *(withName)* — floor 8 GB | `3.h × attempt` | `withName` |
| `GENERATE_PREPROCESS_QC` | `4` | `200 GB` | `4.h × attempt` | `process_medium` |

`BASICPY` is **not** in the table above and cannot be: the guard behind these tables
(`tests/test_resource_label_coverage.py`) reads `modules/local/*.nf`, and `BASICPY` lives in
`modules/nf-core/basicpy/`, vendored unmodified. Its resources are its upstream
`label 'process_single'` (1 cpu, `8.h × attempt`) with `memory` raised to `32 GB × attempt`
by a partial `withName:` override in `conf/modules.config` — it runs Bio-Formats under a JVM
and materialises one channel's tile stack, which does not fit the single tier's 12 GB on a
real slide. Changing either number changes an unguarded figure, so change it here too.

### Registration — VALIS

| Process | `cpus` | `memory` (attempt 1) | `time` | Owner |
|---|---|---|---|---|
| `REGISTER` | `8` | `300 GB × attempt` | `24.h × attempt` | `withName` |

`REGISTER` also carries `maxForks = Math.min(10, params.max_forks)` and its own error
strategy — see [Retry policy](#retry-policy) and
[Execution & concurrency](#execution--concurrency).

### Registration — tiled / STARE

Deliberately small: the tiled backend is JVM-free and tile-streamed, so a few GB
suffices even for large slides. This is what makes `--registration_method tiled`
laptop-viable.

| Process | `cpus` | `memory` (attempt 1) | `time` | Owner | `maxForks` |
|---|---|---|---|---|---|
| `TILED_COARSE` | `2` *(label)* | `8 GB × 2^(attempt−1)` *(withName)* | `2.h × attempt` *(label)* | partial | `20` |
| `TILED_REG_TILE` | `2` *(label)* | derived from `reg_tiled_tile` + 2×`reg_tiled_halo`, `× attempt` *(withName)* — 4 GB at defaults | `2.h × attempt` *(label)* | partial | `20` |
| `TILED_SOLVE` | `1` *(label)* | `1 GB × attempt` *(withName)* | `8.h × attempt` *(label)* | partial | — |
| `TILED_STITCH` | `4` *(label)* | derived from `reg_tiled_out_tile`, `× attempt` *(withName)* — 4 GB at defaults | `4.h × attempt` *(label)* | partial | `10` |

`TILED_COARSE` / `TILED_REG_TILE` / `TILED_SOLVE` / `TILED_STITCH` are the STARE method —
the only shape it has.

`TILED_REG_TILE`, `TILED_STITCH`, `TILE_FOR_BASIC`, `APPLY_PROFILES` and
`MERGE_AND_PYRAMID` are the
processes whose memory
request is **derived from a parameter** instead of being a constant: the first
scales with `reg_tiled_tile + 2 × reg_tiled_halo`, the second with
`reg_tiled_out_tile`, each the measured linear fit doubled and floored at 4 GB.
Raising a tile size therefore raises the reservation rather than producing a
SIGKILL. The arithmetic is written out inside each process' own
`memory = { … }` closure in `conf/modules.config`, immediately under the block
comment that derives it; the two closures are near-identical and that duplication
is forced — Nextflow 26's strict config parser rejects a function declaration in
a config file, so there is no legal way to share a helper between them.

`MERGE_AND_PYRAMID` joined that list when it stopped holding the slide. It used
to ask for a flat 200 or 300 GB on a tier over the summed channel files, because
it allocated the whole `(C, H, W)` stack before writing anything. It now streams
the base resolution from a generator of tiles, so its request is built from **one
decoded plane** — estimated as 4× the largest single channel file, since a
config closure cannot know the compression ratio — plus the pyramid levels that
must stay resident while `tifffile` fills the SubIFDs one level at a time. That
second term is a geometric series in `pyramid_scale` and disappears below three
`pyramid_resolutions`, which is why both parameters appear in the row. Floor 8 GB.

Both `APPLY_PROFILES`' write-tile-buffer term and `MERGE_AND_PYRAMID`'s
decoded-plane term assume tifffile's compressor pool is pinned to `maxworkers=1`
on the write itself (`bin/apply_basic_profiles.py`,
`bin/merge_channels_pyramid.py:826-847`) — without that pin, the container's
tifffile version reintroduces a term that scales with the channel count, which
these figures do not budget for. See the `maxworkers=1` comment in each
process' `conf/modules.config` block for the measured numbers.

The 4× is a **floor estimate, not a bound**, and the block comment in
`conf/modules.config` records the measured counterexamples: zlib at
SPLIT_CHANNELS' settings reaches 4.2× on a plane that is 75 % true-black
background, and 796× on a near-empty channel. A WSI is mostly empty glass and
every channel shares that background, so taking the largest file does not rescue
the estimate. What backstops it is `conf/base.config`'s exit-137 retry with
`maxRetries = 3` against a request that is multiplied by `task.attempt` — four
attempts cover a shortfall of up to 4×, and beyond that the task fails loudly.
Measure the real ratio against a production `channels/` directory and raise the
coefficient if one becomes available.

The "4 GB at defaults" figures above are the shipped-default evaluation of those
formulas, not independent constants — the **parameter names**, not the numbers,
are what `tests/test_resource_label_coverage.py` checks for all five of these
param-derived rows.

The STARE method's memory is bounded. Measured peak RSS on a 16384² 2-channel
tiled OME-TIFF:
`TILED_COARSE` 0.91 GB, `TILED_REG_TILE` 1.31 GB, `TILED_SOLVE` < 1.31 GB,
`TILED_STITCH` 1.35 GB — each set by a parameter (`reg_tiled_coarse_max_dim`,
`reg_tiled_tile` + `reg_tiled_halo`, `reg_tiled_out_tile`) rather than by slide
dimensions. A single-task `TILED_REGISTER` alternative used to exist behind a flag; it had
no such bound (both whole slides, an all-channel float32 copy and the full warped output
live at once, budgeted from file size), so it was removed rather than kept as an unbounded
opt-out.

### Registration QC

| Process | `cpus` | `memory` (attempt 1) | `time` | Owner |
|---|---|---|---|---|
| `GENERATE_REGISTRATION_QC` | `1` *(withName)* | tier on `registered + reference`: `f<20` → 100, `f<50` → 200, else 300 GB, `× attempt` | `12.h × attempt` *(label `process_high`)* | partial |
| `SEG_QC_SEGMENT` | `8` | tier on image: `f<10` → 32, `f<30` → 64, else 128 GB, `× attempt` | `4.h × attempt` | `withName` (`SEGMENT`'s, matched via the alias) |
| `SEG_QC_GEOJSON` | `1` | `64 GB × attempt` | `4.h × attempt` | `withName` |
| `WARP_SEG_QC` | `2` | `32 GB × 2^(attempt−1)` → 32 / 64 / 128 / 256 | `3.h × attempt` | `withName` |

`GENERATE_REGISTRATION_QC` is the one process whose tier is keyed on the
**combined** size of two inputs (the registered image *and* the reference), not
on a single file — it holds both to build the overlay.

`WARP_SEG_QC` uses a *doubling* ramp rather than a linear one. Its historical
exit-140 kills came from rasterizing both slides' polygons onto a whole-slide
label grid; the staged design scores each pair inside its own bounding box, so
peak RAM is now one nucleus regardless of slide size. 32 GB is generous, and the
ramp exists only for pathological inputs. Runtime, not memory, is the binding
constraint.

### Segmentation

| Process | `cpus` | `memory` (attempt 1) | `time` | Owner |
|---|---|---|---|---|
| `SEGMENT` | `8` | tier: `f<10` → 32, `f<30` → 64, else 128 GB, `× attempt` | `4.h × attempt` | `withName` |
| `EXTRACT_CELL_PROPERTIES` | `1` | `64 GB × attempt` | `12.h × attempt` | `withName` |
| `EXTRACT_NUCLEI_PROPERTIES` | `1` | `64 GB × attempt` | `12.h × attempt` | `withName` |
| `EXTRACT_MASK_SERIES` | `2` | `32 GB × attempt` | `2.h × attempt` | `process_low` |
| `SEG_QUALITY_EVAL` | `8` | tier on image: `f<10` → 128, `f<30` → 256, else 448 GB, `× attempt` | `4.h × attempt` | `withName` |

`SEGMENT` asks for 8 CPUs so a CPU-only path — and the CPU-bound label expansion
and Dask tiling either side of inference — stays tolerable. GPU inference is
unaffected by that number.

`SEG_QUALITY_EVAL` is opt-in (`-params-file params/seg_quality_eval.json`) and sized off the
image rather than the mask: CSE's cost is driven by per-pixel index structures on
the DECOMPRESSED masks, so a well-compressed WSI needs far more RAM than its file
size suggests. `--cse_max_pixels` bins the input to cap that; both it and
`SEG_QUALITY_EVAL` retry three times before dropping, and the drop is logged
rather than silent — see [Retry policy](#retry-policy).

### Postprocessing

| Process | `cpus` | `memory` (attempt 1) | `time` | Owner |
|---|---|---|---|---|
| `SPLIT_CHANNELS` | `1` | tier: `f<5` → 32, `f<15` → 64, else 128 GB, `× attempt` | `2.h × attempt` | `withName` |
| `QUANTIFY` | `1` | `128 GB × attempt` | `12.h × attempt` | `withName` |
| `MERGE_QUANT_CSVS` | `2` | `32 GB × attempt` | `2.h × attempt` | `process_low` |
| `EXPORT_GEOJSON` | `1` | `32 GB × attempt` | `2.h × attempt` | `withName` |
| `MERGE_AND_PYRAMID` | `2` | derived from the largest single channel file + `pyramid_resolutions` and `pyramid_scale`, `× attempt` *(withName)* — floor 8 GB | `8.h × attempt` | `withName` |
| `EXPORT_SPATIALDATA` | `4` *(label)* | `200 GB` *(label)* | `4.h × attempt` *(withName)* | partial |
| `GENERATE_POSTPROCESSING_QC` | `4` | `200 GB` | `4.h × attempt` | `process_medium` |

`EXPORT_SPATIALDATA`'s label is chosen at runtime —
`params.spatialdata_include_image ? 'process_high' : 'process_medium'`. The row
above is the default (`false` → `process_medium`); with `--spatialdata_include_image`
it asks for `8` cpus and `300 GB` instead.

### Run-level

| Process | `cpus` | `memory` (attempt 1) | `time` | Owner |
|---|---|---|---|---|
| `GENERATE_QC_REPORT` | `2` | `32 GB × attempt` | `2.h × attempt` | `process_low` |
| `AGGREGATE_SIZE_LOGS` | `1` | `12 GB × attempt` | `8.h × attempt` | `process_single` |
| `MERGE_SEG_EVAL` | `1` | `4 GB × attempt` | `1.h × attempt` | `withName` |

---

## Global ceilings

Every request is clamped by `process.resourceLimits`, which reads three
parameters:

| Parameter | Default | Effect |
|---|---|---|
| `max_cpus` | `128` | Upper bound on any process's `cpus` |
| `max_memory` | `700.GB` | Upper bound on any process's `memory` |
| `max_time` | `240.h` | Upper bound on any process's `time` |

Profiles override these:

| Profile | `max_cpus` | `max_memory` | `max_time` |
|---|---|---|---|
| *(none)* | `128` | `700.GB` | `240.h` |
| `local` | `4` | `16.GB` | `72.h` |
| `ieo` | `128` | `700.GB` | `240.h` |
| `test` | see `conf/test.config` | | |

!!! warning "`resourceLimits` is a closure for a reason"
    The top-level `process.resourceLimits` in `nextflow.config` is a **closure**,
    not a plain map, so it is evaluated at task-submission time — *after* the
    whole profile stack has merged. A plain map would eagerly capture
    `params.max_*` as they stand at that point in the file (128 / 700 GB / 240 h),
    silently ignoring a profile's override and *raising* the ceiling instead of
    enforcing it.

    The `slurm` profile is the one exception: it assigns `resourceLimits` as a
    plain map, which shadows the closure and freezes the values. Combining
    `-profile slurm,ieo` therefore will **not** pick up `ieo`'s pinned `max_*`.
    Today the two coincide, so there is no observed bug — but no documented
    invocation combines `slurm` with another profile that sets `max_*`.

---

## Retry policy

### Default (`conf/base.config`)

`maxRetries = 3`. A task retries when its exit status is one of:

| Exit | Meaning |
|---|---|
| `104` | Connection reset |
| `134` | `SIGABRT` |
| `135` | `SIGBUS` |
| `137` | `SIGKILL` — usually the OOM killer |
| `139` | `SIGSEGV` |
| `140` | `SIGUSR2` — SLURM's pre-walltime warning |
| `143` | `SIGTERM` — job killed |

Anything else is `finish`: the pipeline stops submitting new work and lets
running tasks complete.

Because memory and time both scale with `task.attempt`, a retry after an OOM
kill automatically climbs the ramp.

### `REGISTER` — retries on exit 1 as well

VALIS tile-read failures and JVM out-of-heap conditions surface as a plain
exit 1, which the default strategy would treat as fatal. `REGISTER` therefore
retries on `[1, 104, 134, 135, 137, 139, 140, 143]`.

### QC processes — never gating

`GENERATE_PREPROCESS_QC`, `GENERATE_REGISTRATION_QC`,
`GENERATE_POSTPROCESSING_QC`, `GENERATE_QC_REPORT`, `SEG_QC_SEGMENT`,
`SEG_QC_GEOJSON` and `WARP_SEG_QC` share one policy: retry transient failures up to `maxRetries`, then
**`ignore` rather than `finish`**.

```groovy
errorStrategy = { task.exitStatus in ((130..145) + 104) && task.attempt <= 3 ? 'retry' : 'ignore' }
```

`130..145` is the whole "killed by a signal" range, not a hand-picked subset —
an earlier enumerated set threw away the retry budget on any signal it had
missed.

!!! danger "A silently missing QC artifact is by design — and can hide a real failure"
    QC outputs are aggregated with `collect()` / `collectFile()` / `combine()` /
    `join()`, none of which require a fixed item count, so a missing QC output is
    simply absent and never deadlocks the DAG. That is the point: a broken QC
    step must never stop a multi-day run.

    The cost is that a genuine OOM or walltime kill in one of these processes is
    swallowed after the retry budget. If a QC artifact you expected is missing,
    check `.trace/trace.txt` for the task's exit status rather than assuming the
    step was skipped.

---

## GPU

`SEGMENT` requests a GPU when `seg_gpu = true` (the default), and so does
`SEG_QC_SEGMENT` — it is the same process under an alias, and Nextflow matches
`withName: 'SEGMENT'` against an alias' original name. `SEG_QC_GEOJSON` does not:
it only traces contours, which is pure CPU.

| Engine | What is added |
|---|---|
| SLURM | `clusterOptions = --gres=gpu:${params.gpu_type}` — default `nvidia_h200:1` |
| Docker | `containerOptions = --gpus all` |
| Singularity | `containerOptions = --nv` |

Match `--gpu_type` to your cluster's GRES string (`sinfo -o "%G"`). Without
`--nv`, Singularity does not bind the host NVIDIA driver stack and
`torch.cuda.is_available()` is `False` even when SLURM has granted the GPU —
CellSAM then silently falls back to CPU, which turns a WSI run into a multi-day
job.

Set `seg_gpu = false` to force CPU; the `--gres` request and the container flags
are both dropped.

---

## Execution & concurrency

| Setting | Value | Where |
|---|---|---|
| `process.maxForks` | `params.max_forks` (`100`) | `nextflow.config` |
| `process.stageInMode` | `symlink` | `nextflow.config` — zero-overhead, works cross-filesystem |
| `executor.queueSize` | `params.queue_size` (`20`) | `nextflow.config` — max concurrent scheduler submissions |
| `executor.exitReadTimeout` | `1 day` | `conf/base.config` — SLURM status-poll timeout |

Both are tunable from the command line: `--max_forks` and `--queue_size`.

**They are a pair, and tuning one alone is usually a no-op.** `max_forks` caps how many
tasks of any ONE process run at once; `queue_size` caps how many run at once across the
WHOLE pipeline. The lower binds, and at the shipped defaults `queue_size` (20) is far below
`max_forks` (100) — so raising `max_forks` on its own changes nothing. Raise both, or raise
`queue_size` alone if you simply want more total concurrency.

Per-process `maxForks` overrides: `REGISTER`, `TILED_STITCH` at `10`; `TILED_COARSE` /
`TILED_REG_TILE` at `20`. These bound how many memory-heavy registration tasks can be in
flight at once. Each is written `Math.min(<its own limit>, params.max_forks)`, so
**lowering** `--max_forks` really does throttle every module, while **raising** it never
lifts one of these past the limit its own block sets for its own reasons. Measured on the
test profile: at the default, `REGISTER` runs at 10 and everything else at 100; at
`--max_forks 4` every process runs at 4; at `--max_forks 50`, `REGISTER` stays at 10.

`executor.queueSize` is assigned in `nextflow.config`, not in `conf/base.config` where the
rest of the executor scope lives. That is deliberate and load-bearing — see
[Why the includes sit after the params block](#why-the-includes-sit-after-the-params-block).

### Why the includes sit after the params block

`conf/base.config` and `conf/modules.config` are included **after** `nextflow.config`'s
`params` block, not at the top of the file. Anything in those files that reads `params.*`
depends on it:

* A `params.x` reference evaluated **before** the params block exists does not read `null`.
  Nextflow resolves it to an empty `ConfigObject` — a Map. Inside a closure that is
  harmless, because closures run at task-submission time, which is why every
  `memory = { ... }` closure worked even when the includes sat at the top. Evaluated
  eagerly it is not: `params.x as int` on a Map throws *"Cannot coerce a map to class
  java.lang.Integer"* and the entire config fails to parse.
* Inside an `executor { }` scope it is worse than an error. `queueSize = params.queue_size`
  parsed from an early-included file is read as the opening of a **nested scope named
  `params`**, and the `queueSize` setting vanishes from the resolved config with no error
  at all — silently falling back to Nextflow's own default.
* `maxForks` cannot dodge this the way `memory` does, because it is **not a dynamic
  directive**: Nextflow compares it against `0` in `TaskProcessor`'s constructor, so a
  closure throws *"Cannot compare ... Closure ... and java.lang.Integer with value '0'"*.

Relative order is otherwise unchanged — both files are still included before the `executor`
and `process` blocks, so those still take precedence. Verified by diffing `nextflow config`
for the `test`, `test_full` and `local` profiles across the move: the only content
difference is the two new parameters. Guarded by `tests/test_concurrency_params.py`.

---

## Containers

Every process pins a fixed image tag — never `:latest`. The `bolt3x/mirage-*` image
NAMES (one Docker Hub repository per image, e.g. `bolt3x/mirage-preprocess`,
`bolt3x/mirage-tiled`) are content-descriptive; the TAG on each is an immutable
SemVer version (`1.0.0`), tied to `manifest.version` — see
[Installation → Pre-pulling container images](installation.md#pre-pulling-container-images-optional).

| Image | Processes |
|---|---|
| `bolt3x/mirage-convert:1.0.0` | `CONVERT_IMAGE` |
| `bolt3x/mirage-preprocess:1.0.0` | `TILE_FOR_BASIC`, `APPLY_PROFILES`, `SPLIT_CHANNELS`, `GENERATE_PREPROCESS_QC`, `GENERATE_QC_REPORT` |
| `docker.io/labsyspharm/basicpy-docker-mcmicro:1.2.0-patch5` | `BASICPY` (vendored nf-core module; pulls its own image, and errors under `-profile conda`) |
| `cdgatenbee/valis-wsi:1.0.0` | `REGISTER` |
| `bolt3x/mirage-tiled:1.0.0` | `TILED_COARSE`, `TILED_REG_TILE`, `TILED_SOLVE`, `TILED_STITCH` |
| `bolt3x/mirage-regqc:1.0.0` | `GENERATE_REGISTRATION_QC` |
| `bolt3x/mirage-stardist:1.0.0` | `SEGMENT` / `SEG_QC_SEGMENT` when `--seg_method stardist` |
| `bolt3x/mirage-instanseg:1.0.0` | `SEGMENT` / `SEG_QC_SEGMENT` when `--seg_method instantseg` *(default)* |
| `bolt3x/mirage-cellsam:1.0.0` | `SEGMENT` / `SEG_QC_SEGMENT` when `--seg_method cellsam` |
| *(per backend, `lib/WarpBackends.groovy`)* | `WARP_SEG_QC` |
| `bolt3x/mirage-quantify:1.0.0` | `SEG_QC_GEOJSON`, `QUANTIFY`, `MERGE_QUANT_CSVS`, `EXTRACT_CELL_PROPERTIES`, `EXTRACT_NUCLEI_PROPERTIES`, `EXPORT_GEOJSON`, `GENERATE_POSTPROCESSING_QC` |
| `bolt3x/mirage-merge:1.0.0` | `MERGE_AND_PYRAMID`, `EXTRACT_MASK_SERIES` |
| `bolt3x/mirage-spatialdata:1.0.0` | `EXPORT_SPATIALDATA` |
| `ubuntu:22.04` | `AGGREGATE_SIZE_LOGS` |

`SEGMENT` and `WARP_SEG_QC` resolve their image from a backend table
(`lib/SegBackends.groovy`, `lib/WarpBackends.groovy`) rather than a literal, so
the container follows `--seg_method` and the registration method respectively.
An unrecognised `--seg_method` is rejected by name rather than silently falling
back to a different segmenter.

---

## Measuring what a run actually used

With `--enable_trace` (default on), every process emits a per-task input-size
log, `AGGREGATE_SIZE_LOGS` collates them to `<outdir>/size_logs/input_sizes.csv`,
and Nextflow writes `.trace/trace.txt` with `peak_rss`, `peak_vmem`, `realtime`
and retry counts.

`<outdir>/qc/mirage_resource_report.html` joins the two into run totals, a
per-process rollup, resource-vs-input-size fits, the top-N heaviest and slowest
tasks, and a retry/failure list. Re-runnable by hand:

```bash
python bin/generate_resource_report.py \
  --trace .trace/trace.txt \
  --size-log results/size_logs/input_sizes.csv \
  --output resource_report.html
```

That table is the right starting point for lowering a `withName:` request that
is over-provisioned for your data.

---

## See also

- :material-tune: **Every parameter** — [Parameters](parameters.md)
- :material-file-tree: **Every input and output** — [Inputs & outputs](outputs.md)
- :material-console: **Cluster invocations** — [Usage → Running on HPC](usage.md#running-on-hpc)
