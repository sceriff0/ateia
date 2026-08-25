# Migration note — 2026-08-24 / 2026-08-25

<p class="standfirst">Six changes that alter what a run produces or what its outputs mean. None of
them is a bug fix you can ignore: each one changes numbers, files, or invocations that something
downstream already depends on. Read this before comparing any post-merge output against a
pre-merge one.</p>

!!! danger "The common failure this note exists to prevent"
    Every item below is silent from the consumer's side. Nothing in a run's output
    says "this was corrected differently", "this key is absent on purpose", or
    "this directory was never written". A comparison across the merge that does not
    account for these is measuring the change, not the biology.

---

## 1. Darkfield correction is gone

Illumination correction now runs through the vendored nf-core `BASICPY` module at
its upstream default `get_darkfield = False`. The deleted in-house path ran
`BaSiC(get_darkfield=True, smoothness_flatfield=1)`.

**Corrected pixel values are not comparable across the merge.** At `False`,
basicpy estimates and removes a flatfield only; no additive darkfield term is
estimated or subtracted. Anyone comparing intensities — raw, normalised, or
z-scored — between a pre-merge run and a post-merge run is comparing two
different corrections, and the difference is a per-pixel offset field, not a
scalar.

**What to do.** Re-run the correction step for any cohort whose intensities will
be compared across the merge. Do not mix corrected outputs from the two eras in
one analysis. The full comparison of the two parameterisations, and why
`get_darkfield` is a safety property rather than a tuning knob, is in
`modules/nf-core/basicpy/MIRAGE-NOTES.md`.

---

## 2. NaN measurements are omitted, not written as `0.0`

`bin/export_geojson.py` writes a measurement only when its value is present
(`if pd.notna(val)`). A cell with no nuclear overlap therefore carries **fewer
measurement keys than its neighbours**, rather than carrying them with a value
of `0.0`.

This is correct — a cell with no nucleus has no nuclear median, and `0.0` is a
measurement, not an absence — but it changes the shape of every feature vector a
consumer builds.

**Who breaks.** Any code indexing a measurement directly rather than through
`.get()`:

```python
# breaks: KeyError on a cell with no nuclear overlap
v = measurements["CD3: Nucleus: Median"]

# correct: absence is a real, expected state
v = measurements.get("CD3: Nucleus: Median")
if v is None:
    ...          # this cell has no nucleus; it is not a zero
```

**The key grammar is unchanged and case-sensitive.** Keys are built by
`bin/utils/measurements.py::measurement_key` as:

```
"<marker>: <Compartment>: <Statistic>"
```

with **exactly** one space after each colon, `Compartment` one of
`Nucleus`, `Cytoplasm`, `Cell`, and `Statistic` one of `Median`, `Mean`, `Sum`.
Three compartments × three statistics is nine keys per marker; a cell with no
nuclear overlap carries the six non-nuclear ones and, per marker, drops the three
`Nucleus` keys — so **7 of 9** for a single-marker panel counting the two
compartments that survive plus morphology, and in general "the `Nucleus` third is
absent".

Morphology keys behave the same way: `Eccentricity`, `Perimeter µm`, `Solidity`,
`Convex Area µm²`, `Major Axis Length µm`, `Minor Axis Length µm` and `Area µm²`
are each written only when present.

**Downstream:** `qupath-extension-flowpath` consumes `cells.geojson`. See
[Notifying the consumer](#notifying-the-consumer) below.

---

## 3. STARE meshes differ from the pre-merge ones, by design

The STARE registration backend gained two gates on its control points — a
confidence gate on the discarded correlation error, and a residual range gate.
Both **reject control points the pre-merge code accepted**.

**Registration output is not bit-comparable across the merge**, and neither are
the meshes, the warped images, or anything derived from them. The rejected points
were the ones the gates were added to reject, so this is an improvement, not a
drift — but a diff will not be empty and should not be expected to be.

---

## 4. Checkpoint CSVs carry an `id` column

Every checkpoint schema in `lib/Checkpoint.groovy` gained an `id` column, and the
readers read it back rather than re-deriving identity from a basename. A
checkpoint written before this column existed has no `id` field at all, and
`Meta.fromCheckpointRow` detects exactly that shape and **fails with a message
naming the fix** rather than silently falling back to a re-derived — and possibly
different — id.

**Who breaks.** An existing `--prior_outdir` used with `--mode add_cycle`, or an
existing output tree used with `--start <step>`, both fail loudly at launch
against a pre-change checkpoint.

**What to do.** Re-run the step that wrote the checkpoint. This is intended
behaviour: a checkpoint names a derived artifact whose basename cannot reproduce
the identity the samplesheet row was originally assigned, so re-deriving it would
manufacture a different identity depending on which checkpoint happened to be the
entry point.

---

## 5. A run publishes final outputs only, and deletes its work directory

Two new defaults, from a 2026-08-25 decision:

| Param | Was | Now |
|---|---|---|
| `cleanup_level` | *(did not exist)* | `'final'` |
| `cleanup_work` | `false` | `true` |

At `--cleanup_level=final` a run publishes `pyramid/`, `geojson/`,
`quantification/` and `spatialdata/` per patient, plus run-level `qc/`, `csv/` and
`size_logs/`. **Everything else is never published** — not published and then
deleted: every `publishDir` is `mode: 'copy'`, so publish-then-delete would pay a
full copy out of `work/` for a file nobody reads.

**Who breaks.**

* Anything reading `<outdir>/<pid>/registered/`, `segmentation/`,
  `split_channels/`, `quantify/`, `cell_properties/`, `converted/` or
  `preprocessed/` from a default run — those directories are not written.
* Anything reading `<outdir>/csv/*.csv`. **No checkpoint manifest is written at a
  cleaning level**, because its rows would name files that were never published.
  `csv/README.txt` says so in the directory itself.
* `--start <step>` against a default run's output.
* A `-resume` after a successful run: `cleanup_work` empties the work directory,
  so there is no cache. The pipeline warns at launch when both are in play.

**What to do.** Pass `--cleanup_level none` on any run whose output will be
re-entered — a run you intend to `--start` from, or to use as a `--prior_outdir`.
`--mode add_cycle` is **refused at launch** at any other level, so the mistake can
only be made on the first, ordinary run of a cyclic-IF series. See
[Output cleanup](parameters.md#output-cleanup) and
[add_cycle](add_cycle.md#prerequisites).

---

## 6. Two invocations that used to be accepted now fail at launch

* `--seg_instantseg_target` accepts only `all_outputs`. `cells` and `nuclei` each
  return a **single** label map, which `bin/segment_instantseg.py` used to
  replicate into both the nuclei and the cell mask — making
  `Cytoplasm = Cell − Nucleus` empty for every cell and every cytoplasmic
  measurement silently zero. The reader refuses a single map, and the schema enum
  is narrowed so the refusal happens at launch rather than inside a segmentation
  task. The shipped default was already `all_outputs`, so no default run changes.
* An OME header whose `PhysicalSizeXUnit` this pipeline does not recognise now
  **raises** in `bin/ashlar_retile.py` instead of falling back to
  `0.325 µm/px`. A recognised non-µm unit (`nm`, `mm`, …) is now **converted**
  rather than read as though it were µm — a header in nm was previously a 1000×
  scale error straight into ASHLAR's `--maximum-shift`. Runs whose inputs carry a
  non-µm header produce different — correct — registration than they did before.

---

## Notifying the consumer

`qupath-extension-flowpath` consumes `cells.geojson` and is affected by item 2.
The change it needs is one line per read site:

```diff
- val = measurements["CD3: Nucleus: Median"]
+ val = measurements.get("CD3: Nucleus: Median")
```

with the `None` case handled as "this cell has no nucleus", never as zero.

**This has not been filed.** Opening the issue is a push to a sibling repository
and needs the maintainer's go-ahead; it is listed here so it is not forgotten.
