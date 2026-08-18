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
  <div class="g"><div class="k">case 1</div><div class="v">withName owns all three</div><div class="d">No label. The block sets cpus, memory and time. 13 processes.</div></div>
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
| `TILE_FOR_BASIC` | `2` | derived from `preproc_tile_size` + one decoded source plane, `× attempt` *(withName)* — floor 6 GB | `2.h × attempt` | `withName` |
| `APPLY_PROFILES` | `2` | derived from `preproc_tile_size` + one decoded source plane, `× attempt` *(withName)* — floor 8 GB | `3.h × attempt` | `withName` |
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

`REGISTER` also carries `maxForks = 10` and its own error strategy — see
[Retry policy](#retry-policy).

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

`TILED_REG_TILE`, `TILED_STITCH`, `TILE_FOR_BASIC` and `APPLY_PROFILES` are the
processes whose memory
request is **derived from a parameter** instead of being a constant: the first
scales with `reg_tiled_tile + 2 × reg_tiled_halo`, the second with
`reg_tiled_out_tile`, each the measured linear fit doubled and floored at 4 GB.
Raising a tile size therefore raises the reservation rather than producing a
SIGKILL. The arithmetic is written out inside each process' own
`memory = { … }` closure in `conf/modules.config`, immediately under the block
comment that derives it; the two closures are near-identical and that duplication
is forced — Nextflow 26's strict config parser rejects a function declaration in
a config file, so there is no legal way to share a helper between them. The
"4 GB at defaults" figures above are the shipped-default evaluation of those
formulas, not independent constants — the **parameter names**, not the numbers,
are what `tests/test_resource_label_coverage.py` checks for these two rows.

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

`SEGMENT` asks for 8 CPUs so a CPU-only path — and the CPU-bound label expansion
and Dask tiling either side of inference — stays tolerable. GPU inference is
unaffected by that number.

### Postprocessing

| Process | `cpus` | `memory` (attempt 1) | `time` | Owner |
|---|---|---|---|---|
| `SPLIT_CHANNELS` | `1` | tier: `f<5` → 32, `f<15` → 64, else 128 GB, `× attempt` | `2.h × attempt` | `withName` |
| `QUANTIFY` | `1` | `128 GB × attempt` | `12.h × attempt` | `withName` |
| `MERGE_QUANT_CSVS` | `2` | `32 GB × attempt` | `2.h × attempt` | `process_low` |
| `EXPORT_GEOJSON` | `1` | `32 GB × attempt` | `2.h × attempt` | `withName` |
| `MERGE_AND_PYRAMID` | `2` | tier on channels + masks: `f<20` → 200, else 300 GB, `× attempt` | `8.h × attempt` | `withName` |
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
| `process.maxForks` | `100` | `nextflow.config` |
| `process.stageInMode` | `symlink` | `nextflow.config` — zero-overhead, works cross-filesystem |
| `executor.queueSize` | `20` | `conf/base.config` — max concurrent scheduler submissions |
| `executor.exitReadTimeout` | `1 day` | `conf/base.config` — SLURM status-poll timeout |

Per-process `maxForks` overrides: `REGISTER`, `TILED_STITCH`
at `10`; `TILED_COARSE` / `TILED_REG_TILE` at `20`. These bound how many
memory-heavy registration tasks can be in flight at once.

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
