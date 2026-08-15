# The meta map contract

Every channel in this pipeline carries `[meta, file(s)]` tuples. `meta` is a plain Groovy
`Map`, and this page is the **canonical list of the keys it may hold** — what each one
means, who attaches it, and who reads it.

!!! info "Why this page is tracked"
    The contract used to be described only in the repo's `CLAUDE.md`, which
    `.gitignore` excludes — so it was absent from every checkout and every worktree, and
    the description that existed listed eight keys while the code attached ten. This page
    is tied to the code by `tests/test_meta_key_documentation.py`, which fails in **both**
    directions: a key attached in code and missing from the table below fails, and a row
    below naming a key nothing attaches or reads fails too.

## The keys

| Key | Attached by | Meaning and who reads it |
|---|---|---|
| `patient_id` | `CsvUtils.parseMetadata` — the samplesheet's `patient_id` column | The grouping key for the whole pipeline. Every `groupTuple`/`combine`, every process `tag`, and every published path (`Layout`) is rooted on it. Present on every meta from the samplesheet read onward. |
| `is_reference` | `CsvUtils.parseMetadata` (`parseIsReference`, strict `true`/`false`); filled in by `registration.nf`'s grouping closure under `--allow_auto_reference`; set explicitly by `add_cycle.nf` on the frozen prior reference | Marks the one slide per patient every other slide is registered onto. Read by `seg_qc.nf` (reference/moving branch), by `SPLIT_CHANNELS` (only the reference keeps its nuclear channel), and by the registration grouping. |
| `channels` | `CsvUtils.parseMetadata` — the samplesheet's `channels` column, `\|`-separated; **rebound** by `preprocess.nf` to the channels that survive preprocessing | The slide's marker list, in declared order. Read by `PanelSignature` (a slide's within-patient identity), by the `channels` column of `csv/registered.csv`, and by every nuclear-marker decision via `MarkerUtils`. **Shared list reference:** `meta + [k: v]` is clone-then-`putAll`, so derived metas share this list — use `toSorted()`, never in-place `sort()`. |
| `id` | `CsvUtils.imageId` via `input_check.nf` (the image stem, patient-prefixed); rebound by `quantify_markers.nf` to `<patient_id>_<channel>` per split channel, and by `add_cycle.nf` to `<patient_id>_reference` | The per-image unique id, and the only key guaranteed distinct within a patient. It drives output file naming, and it is `SEG_QC`'s per-slide join key. See `CsvUtils.imageId`'s docblock — "THE ONE RULE, NOT A CONVENTION". |
| `images_count` | `input_check.nf`, from `CsvUtils.countImagesPerPatient` | How many slides this patient has. The `groupKey(patient_id, images_count)` size hint that lets registration group by patient **streaming**, without buffering the whole run. Read by `registration.nf` and `preprocess.nf`. |
| `channels_count` | `input_check.nf`, from `CsvUtils.countChannelsPerPatient` | How many split-channel files this patient will produce (post nuclear-drop, so exact). The size hint for the quantification and postprocessing groupings. Read by `quantify_markers.nf`, `postprocess.nf`, `final_qc.nf`. |
| `channel_name` | `quantify_markers.nf`, from the split channel TIFF's basename | The single marker a per-channel task is quantifying. Read by `QUANTIFY` and by the merge that reassembles one row per cell. |
| `qc_slide` | `seg_qc.nf`, from the native image's filename with `.ome.tif(f)`/`.tif(f)` stripped | The slide name `WARP_SEG_QC` looks the slide up by, and it must equal VALIS's own `registrar.slide_dict` key (`valtils.get_name`'s convention). Resolved once, on `meta`, because it cannot be recovered from the mask filename — the three segmentation backends name masks differently. |
| `is_passthrough` | `register_patient.nf` (a single-slide patient's reference) and `adapters/tiled_adapter.nf` (every patient's reference) | "This slide reached the registered stream without being warped." Read by `register_patient.nf` to route it through `PUBLISH_PASSTHROUGH`, so it lands in `<pid>/registered/registered_slides/` like every other row of `csv/registered.csv`. It selects a **process**, never a published path — see `Layout`'s note. |
| `tiles_count` | `adapters/tiled_adapter.nf`, from the slide's tile plan | How many tiles this slide was cut into. The `groupKey` size hint for the `TILED_REG_TILE` fan-in, so `TILED_SOLVE` can start on a slide as soon as its own tiles finish. Refused if it would be zero. |

## Rules

**Mutate with `meta + [k: v]`, never `meta.clone()`.** Both are clone-then-`putAll` in
Groovy, so they are operationally identical — the `+` form is the convention because it
reads as an expression and cannot be mistaken for a deep copy. Neither is a deep copy:
`meta.channels` stays the *same* `List` object in the new map.

**A key is either on every meta in a channel, or the channel is documented as mixed.**
`patient_id` and `channels` are on every meta; `tiles_count` exists only inside the tiled
adapter; `qc_slide` only downstream of `SEG_QC`'s segmentation step.

**Adding a key means adding a row here.** `tests/test_meta_key_documentation.py` scans
`subworkflows/`, `workflows/`, `modules/` and `lib/` for `<meta> + [key: ...]` additions
and fails on any it cannot find in the table above.
