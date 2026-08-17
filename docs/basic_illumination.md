# Illumination correction: the three-process nf-core BASICPY path

Illumination correction runs as **three** processes, not one:

```
CONVERT_IMAGE ──► TILE_FOR_BASIC ──► BASICPY ──► APPLY_PROFILES ──► <pid>/preprocessed/
                       │  (nf-core, vendored)         ▲
                       └──── slide + positions ───────┘
```

It is three because nf-core's `basicpy` module does two things mirage's in-process
BaSiC call did not:

* **It computes profiles only.** Upstream (mcmicro) applies them downstream inside
  ASHLAR. mirage has no ASHLAR, so `APPLY_PROFILES` is the missing half.
* **It refuses a single-sited image**, by design. Its entrypoint builds the
  field-of-view axis as `istack.stack(I=('M','T','Z')).transpose('C','I','Y','X')` and
  raises `RuntimeError("The image is single sited. Was it saved in the correct way?")`
  when `len(I) < 2`. Every mirage slide is a single stitched plane per channel —
  `SizeM = SizeT = SizeZ = 1` — so it is refused. `TILE_FOR_BASIC` is what makes it
  acceptable.

## TILE_FOR_BASIC

Takes the converted slide and writes mirage's existing non-overlapping FOV grid — the
same `params.preproc_tile_size` grid the in-process path fitted on, via the same
`count_fovs` / `split_image_into_fovs` helpers — as an OME-TIFF whose **tiles occupy the
`Z` axis**.

* Axes are **`CZYX`**, not `ZCYX`. The module iterates channels
  (`for c, channel_stack in enumerate(istack, 1)`) and fits one profile per channel;
  putting tiles on `C` would fit one profile per tile and mix every marker together.
* `len(I) = SizeM × SizeT × SizeZ`, and mirage writes no M and no T, so every site has to
  come from `Z`.
* Edge tiles are zero-padded to the grid's maximum tile size. This is not new — the
  in-process path handed BaSiC the same padded stack — so the fit input is unchanged.
* **A grid of fewer than two tiles is refused**, with a message naming
  `--preproc_tile_size` and the largest value that would work. BaSiC estimates shading
  across a *population* of fields; one field is not an estimate, and failing here beats
  failing inside a vendored container.

### The tile-position sidecar

`<name>_tiles.json`, written beside the tile stack, so `APPLY_PROFILES` reassembles from
recorded positions rather than re-deriving the grid from the FOV size. JSON, so it is
readable and diffable, and the `positions` rows are exactly what `split_image_into_fovs`
returned, so `reconstruct_image_from_fovs` consumes them directly.

| field | meaning |
|---|---|
| `format_version` | `1`. `apply_basic_profiles.py` refuses a version it does not know rather than misreading a renamed field. |
| `source_image`, `source_dtype` | provenance, and the dtype the corrected slide is stored back as |
| `image_shape` | `[H, W]` of the source slide |
| `fov_size`, `n_fovs_y`, `n_fovs_x`, `tile_shape` | the grid, and the padded tile size |
| `channel_names` | resolved against the image's own channel count |
| `profile_channels` | source channel indices **in tile-stack C order** — the map from a fitted profile back to a slide channel |
| `corrected_channels` | source channel indices the profiles are actually applied to |
| `skipped_channels` | the nuclear/fiducial channels |
| `positions` | one `[y, x, h, w]` row per tile, in stack order |

`profile_channels` and `corrected_channels` are separate lists for one reason: a panel
whose every channel is the configured fiducial (a **CELLTOX-only panel is a supported
input**) leaves nothing to correct, but an OME-TIFF with zero channels is not a thing and
the module still has to run. In that case the stack carries every channel while
`corrected_channels` is empty — profiles are fitted and then applied to nothing, and the
slide comes out unchanged. In every other case the two lists are identical.

### The nuclear/fiducial skip lives here, once

`bin/preprocess.py` skipped correction for the nuclear/fiducial channel, and its comment
records that an earlier version tested `"DAPI" in name.upper()` directly and so silently
corrected a configured CELLTOX fiducial — the channel that drives both registration and
segmentation, corrected on one panel and not another.

The decision is now made **once**, in `TILE_FOR_BASIC`, through `utils.metadata.is_nuclear`
(the shared rule, fed by `MarkerUtils.markerList(params.nuclear_markers)` exactly as
`SPLIT_CHANNELS` and `CONVERT_IMAGE` are), and **recorded in the sidecar**.
`APPLY_PROFILES` reads the answer instead of re-deriving it, so the two halves cannot
drift. `params.preproc_skip_nuclear` still switches it off.

## BASICPY

`modules/nf-core/basicpy/`, vendored unmodified. Read
`modules/nf-core/basicpy/MIRAGE-NOTES.md` before changing anything about it. In short:

* It runs at **upstream defaults**. `ext.args` is empty *on purpose* — mirage's previous
  in-process parameters (`BaSiC(get_darkfield=True, smoothness_flatfield=1)`) are
  deliberately not reproduced. `tests/test_basicpy_defaults_are_deliberate.py` and
  `tests/modules/basicpy.nf.test` both fail if a flag appears.
* **`get_darkfield` is False at those defaults, so no darkfield is estimated or removed.**
  That is a change in the correction model, not a tuning difference: only the
  multiplicative flatfield is divided out, and the additive offset mirage used to remove
  stays in the data. basicpy still resizes its zero-initialised darkfield to the tile
  shape, so `*-dfp.ome.tif` is present, correctly shaped and **all zeros** —
  `APPLY_PROFILES` detects that and logs the correction as flatfield-only.
* `--no_autotune` is **inverted** (`action="store_false"`, `default=True`, gated by
  `if not args.no_autotune:`), so *not* passing it is what leaves autotune off. Passing it
  turns autotune on.
* It **errors under `-profile conda` / `-profile mamba`** by design, in both its `script:`
  and its `stub:` block.
* Its version emit is a hardcoded `val("1.2.0")` on a versions **topic** channel, while
  its container is tagged `1.2.0-patch5`. mirage's QC report collects `versions.yml`
  *files*, so **BASICPY does not appear in it**. `TILE_FOR_BASIC` and `APPLY_PROFILES` do,
  so the step is not versionless — only its vendored middle is.

## APPLY_PROFILES

`corrected = (image - darkfield) / flatfield`, per channel, per pseudo-FOV, reassembled
from the sidecar's positions. Three contracts carried over from the in-process path:

* **The fiducial skip**, read from `corrected_channels` (see above).
* **The negative clip.** `bin/utils/validation.py`'s `clip_negative_values`, called once
  on the assembled stack, so its percentage is a whole-image percentage and it emits
  exactly one aggregate line — not one per channel, and not a forked copy of the function.
  Note the *rationale* has changed: `bin/preprocess.py` explained the clip by BaSiC's
  darkfield exceeding a pixel value, and with `get_darkfield=False` that mechanism is
  largely gone. It is kept because the contract is what downstream reads, and because the
  script still subtracts whatever darkfield it is handed.
* **Profile validity, checked before any arithmetic.** The clip cannot catch a degenerate
  profile — `detect_negative_values` tests `data < 0`, which is False for both `nan` and
  `inf`, and `np.round(np.clip(nan)).astype(uint16)` is undefined behaviour that produces
  a plausible-looking integer. So `_read_profile_stack` rejects non-finite entries in
  either profile and a **non-positive flatfield**, on top of the channel-count and
  tile-shape checks. A zero flatfield is a real failure mode and dividing by it is silent.
  It also catches a **swapped pair**: at upstream defaults the darkfield is all zeros, so
  a swap presents an all-zero flatfield.
* **The storage dtype rule**: clip to range, **round** (half-to-even), then cast.
  `.astype()` truncates toward zero, a one-sided −0.5 LSB bias that never averages out.

The output is `<name>_corrected.ome.tif` published to `<outdir>/<pid>/preprocessed/` —
byte-for-byte the same artifact *kind* `PREPROCESS` published, in the same place, so the
`preprocessed` checkpoint row and every downstream consumer are unchanged by the swap.

## What is intentionally not published

The tile stack, the sidecar and the fitted profiles are all intermediates
(`publishDir = [ enabled: false ]`). Publishing the profiles would be defensible QC, but
it needs a new `Layout.PUBLISHED_KINDS` entry and a matching publish leaf; it was left out
rather than added speculatively.
