# FROZEN.md — STARE synthetic ground-truth benchmark, `__version__ = "1.0.0"`

This is the freeze record: it fixes what the measuring instrument **IS**, so
results measured with it later can be compared to each other. It does not
report benchmark results — see the PENDING section, which is not a formality.

## Status at a glance

| | |
|---|---|
| **FROZEN** (built, reviewed, tests green) | generator (`fields.py`, `physics.py`, `texture.py`, `generate.py`, `labels.py`), metrics (`epe`, `gate_roc`/`gate_auc`, `field_quality`, `cost`), the unit-rung driver (`run_unit.py`), the committed experiment plan (`plan.py` + `benchmarks/configs/synthetic_gt.yaml`), the mid-rung runner (`run_mid.sh`) |
| **PENDING** (NOT run — no cluster, no pulled images in this environment) | the mid rung (20480² x 528 rows), the gigapixel rung, ANY competitor score (VALIS, ASHLAR), the ≤8 GB peak-RSS claim |

**The single most important fact in this document: no sweep has executed.**
Every number in Section 1 comes from unit-scale pins, deliberate-failure
drills, or the one wiring test that ran the real STARE stages at 1024px. The
gigapixel peak RSS and whether it breaches 8 GB **does not exist as a
measured number** and is not written down here as one. Section 3 says so
explicitly, on purpose, so this record cannot later be misread as having
measured what it has not.

---

## 1. FROZEN — the instrument

### 1.1 Version and the freeze rule

`benchmarks/stare_bench/__init__.py`: `__version__ = "1.0.0"`, stamped into
every generated `truth.json` as `generator_version`.

**Changing any committed value recorded in this section after this freeze
invalidates comparison with results already reported under `1.0.0`. The
correct response to needing a change is a version bump — bump MINOR for a
change that invalidates comparison with older runs, PATCH for one that does
not (per the package docstring) — never an in-place edit of this document or
of the committed constants it describes.**

### 1.2 The committed sweep

Source: `benchmarks/stare_bench/plan.py` (`BLANK_FRACTIONS`, `build_plan`)
and `benchmarks/configs/synthetic_gt.yaml`.

| Axis | Committed values |
|---|---|
| seeds | `[1, 2, 3]` |
| field families | `[random_fourier]` |
| correlation lengths (px) | `[333.0, 777.0]` |
| amplitudes (px) | `[12.0, 48.0]` |
| blank fractions | `0.00, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00` (11 points) |
| size (mid rung) | `[20480, 20480]` |
| methods | `[tiled, valis, ashlar, identity]` |

- **Total records:** 528 = 3 seeds x 1 family x 2 correlation x 2 amplitude x
  11 blank fractions x 4 methods. `identity` is the do-nothing baseline (zero
  displacement everywhere), not a registration method — see the identity
  baseline note below and Finding 1. Only `tiled`/`valis`/`ashlar` (396 of the
  528 rows) actually invoke the real Nextflow pipeline; `run_mid.sh` scores
  `identity` directly against ground truth, with no pipeline run and no
  transform to find.
- **Unique `pair_id`s:** 132 (each generated image pair is shared across all
  4 methods, so only method varies within a `pair_id`).
- Blank-fraction density is deliberate: 11 points is a subset of the
  21-point grid `tests/test_tile_residual_confidence.py` already uses, chosen
  so 4 of the 11 land inside the 40-70% band where a tile is accepted with a
  wrong displacement — the earlier `[0, .25, .5, .75, 1.0]` sweep placed zero
  samples there.
- `correlation_px` is enforced, not just documented, to avoid rigging the
  benchmark — but NOT by a divisibility rule against `REG_TILED_TILE = 2048`.
  An earlier version of this document (and of `build_plan`) enforced `corr %
  REG_TILED_TILE == 0 or REG_TILED_TILE % corr == 0`, which the design spec
  showed is neither necessary nor sufficient: `correlation_px = 16000` at a
  51200px slide passes that rule while yielding a coarse-grid pitch of
  2047.96px (colliding with STARE's own control-grid spacing), and
  `correlation_px = 2048` at 4096px fails that rule while yielding a
  perfectly safe pitch of 273px. `build_plan` now dry-runs
  `fields.make_field` for every committed `(family, correlation_px)` pair at
  the committed `size` and lets ITS `ValueError` (raised when the *derived*
  coarse-grid pitch is not safely below `MAX_COARSE_PITCH_PX`, a quarter of
  STARE's control-grid tile) be the enforcement — the same check
  `generate_pair` performs at run time, just called earlier so a rigged
  sweep fails at plan-build time. See `plan._validate_fields_are_independent`.
- `amplitude_px` spans STARE's own acceptance gate on purpose — see Finding 1.
- **Identity baseline.** `method = "identity"` scores a predictor that
  returns zero displacement everywhere (`cli.py`'s `score_pair`, before any
  transform or `run_stare` logic). It exists because an absolute EPE number
  is uninterpretable alone: Section 1.6 records STARE scoring
  `ratio(max) = 0.998` against exactly this predictor at the unit rung —
  almost no improvement over doing nothing. Every method's EPE is reported
  alongside this row rather than left to be read as accurate on its own.
- Physics is held fixed across the entire blanking sweep so blank fraction is
  the only variable: `photobleach.factor=0.75`; `noise_and_psf.psf_sigma_px=1.5`,
  `photons=300.0`, `read_sigma=0.01`; `background.af_amplitude=0.12`,
  `seam_px=1024`, `seam_amplitude=0.03`, `focus_sigma_px=0.4`.

`pair_id` encoding note (fixed defect): the amplitude and correlation
segments originally truncated with a bare `int(...)`, which is lossy on
non-integer values. Observed before the fix: `amplitude_px=[12.0, 12.5]` and,
separately, `correlation_px=[333.0, 333.5]`, each collapsed 22 records into
11 unique ids — both collapses produced the identical id
`s1_random_fourier_c333_a012_b000` for what should have been two distinct
experimental conditions. The fix scales by 10 before truncating (one decimal
place of precision), which is more than every committed value above needs.
Dormant in the committed sweep itself (its amplitudes/correlations are
integer-distinct), but real: it silently merges conditions the sweep didn't
intend to merge.

### 1.3 The circularity guard

`labels.py`: `TAU = 0.35` — the minimum fraction of pre-physics signal energy
a tile's post-physics moving crop must retain for that tile to be labelled
"registrable" (the sole ground truth the gate-ROC metric is scored against).

`TAU` is **checked** for consistency against this repo's independently
measured `mov_fg/ref_fg >= 0.4165` separator (`bin/tiled_reg_tile.py`,
documented in `bin/utils/tile_residual.py`), which on real data correctly
classifies 44/68 (64.7%) of accept/reject decisions. `TAU` was never fitted
to that number — the two are drawn from different sources and cross-checked,
not tuned together.

Circularity firewall (`test_field_independence.py`, an independent
`scipy.interpolate.RegularGridInterpolator` fitter, `MIN_RESIDUAL_FRACTION =
0.25`): watched **failing** against a field deliberately built on STARE's own
control grid. Observed residual fraction: **`frac = 0.000` (exact)** for
seeds 1, 2 and 3 — the cheating field's sampler and the independent fitter
share the same node grid and the same boundary-clamp semantics, so nothing is
left unmodelled by construction. Against the real, committed `random_fourier`
field, the guard requires `frac > 0.25` and the field passes that floor.

### 1.4 Retention-map boundary leak (PSF fix)

Measured on the pinned boundary-leak regression test in `physics.py`'s
`noise_and_psf`:

- **Before the fix** (retention treated as purely multiplicative, ignoring
  that a PSF mixes in real signal from non-blanked neighbours across a
  blanked boundary): leaked retention **0.416**.
- **After the fix** (the retention map is blurred with the same kernel used
  on the image itself, since surviving fraction at a blurred pixel reduces to
  the blurred map for locally near-constant intensity): **0.037**, judged a
  ~3.7-sigma read-noise floor tail (`read_sigma=0.01`) rather than leaked
  structure.

### 1.5 Landmark-correspondence direction fix

The defect: `_warp` pull-samples as `mov(p) = ref(p + disp(p))`, so the field
is a function of moving-frame coordinates; landmark generation originally
sampled the wrong endpoint.

- An independent pre-fix check (Ruling 14): sampling `ref` at the landmark's
  `target` coordinates against `mov` at its `moving` coordinates on a
  physics-free pair (no blur/noise/blanking, so any mismatch is purely the
  landmark derivation) gave mean absolute error **0.0275** on a ~1.0 unit
  range under the wrong derivation, versus **0.00011** under the corrected
  one.[^landmark-mae] The comparison method matches the corrected image-content
  test the fix ultimately shipped with, but was run as a standalone check
  during review rather than committed as a test in its own right.
- The committed regression test, reproduced against the reverted buggy code
  as part of the actual fix: MAE **0.0596** (wrong direction) →
  **0.000164** (fixed; residual is interpolation noise).

[^landmark-mae]: Only the `0.0596 → 0.000164` figure is machine-reproducible
today, via `benchmarks/tests/stare_bench/test_generate.py`. The `0.0275 →
0.00011` figure was a one-off check recorded in this project's engineering
ledger (a scratch file under `.superpowers/`, not tracked in this
repository) and cannot be re-run or verified from the current tree — it is
reported here as a second, differently-sourced data point corroborating the
same direction of the fix, not as a claim a reader can check.
- Ground-truth-predictor landmark TRE (`_tre_summary`, Task 2.3, Ruling 26):
  feeding the ground-truth field itself as the predictor —
  - wrong line (`warped = moving - predict(target)`): `mean_px = 4.6407`
    (approximately the field's own 6.0px amplitude — i.e. garbage: a
    perfect predictor scored as if it had done nothing).
  - corrected line (`warped = moving + predict(moving)`): `mean_px = 0.0`
    exactly.

### 1.6 Affine wiring result (unit rung: 1024px image, tile=256)

Re-scoped per Ruling 31 to test **wiring**, not scientific performance —
1024px with a 256px tile gives only a 4x4 mesh, too coarse to host both "the
field is unrepresentable to the mesh" and "the field is recoverable" at once.

- **Affine field** (globally representable — the case this test gates on):
  STARE error max **0.169px** / median **0.096px**, versus an
  identity-baseline (do-nothing) predictor's error max **18.964px** / median
  **9.542px** → **ratio(max) = 0.0089**, clearing the required ≤0.2 bar by
  more than 20x. This confirms the `COARSE → REG_TILE → SOLVE →
  predict_from_manifest` chain is wired correctly end to end.
- **`random_fourier` field at the same rung** (deliberately **not gated** —
  recorded only as an observation, per Ruling 31c): STARE error max
  **6.230px** / median **2.040px**, versus identity max **6.242px** / median
  **2.174px** → **ratio(max) = 0.998**, **ratio(median) = 0.938**. STARE
  performs almost exactly as well as doing nothing on this fixture. This is
  not a bug in the harness — see Finding 1.

---

## 2. Two findings this benchmark exists to surface

### Finding 1 — a genuine limitation of the shipped method (not a fixture artefact)

`bin/tiled_solve.py` refines only control points whose rigid-stage TRE
exceeds `--gate-tre` (`reg_tiled_gate_tre`, default **1.0px**,
`nextflow.config:130`). On any field whose per-tile displacement is largely
sub-pixel after `COARSE` absorbs the lowest-frequency component into a global
affine, the gate zeroes the residual and the mesh collapses toward identity —
STARE then returns approximately its coarse affine. That is exactly what
Section 1.6's `random_fourier` unit-rung ratio (0.998 / 0.938) shows.

This is recorded as a **result about STARE's default configuration**,
because the benchmark exists precisely to surface it: `amplitude_px = 12.0`
in the committed sweep sits near this gate (the interesting, arguably
deficient regime) and `amplitude_px = 48.0` sits well above it (the mesh has
real work to do). The near-gate behavior should be reported at the mid rung,
not tuned out of the fixture.

### Finding 2 — two publishing gaps bound what the mid rung can measure today

1. **VALIS's registrar pickle is never published.** `REGISTER` emits
   `*_registrar.pickle` in-process; `conf/modules.config`'s `REGISTER`
   `publishDir` matches only `*.csv` and the registered OME-TIFFs. The pickle
   never reaches disk, so the VALIS arm cannot be scored at the mid rung.
2. **STARE's per-tile control-point JSONs are never published.**
   `TILED_REG_TILE`'s `publishDir` is `[enabled: false]`. Those JSONs
   (`error`/`ref_fg`/`mov_fg`) are the only record of STARE's per-tile
   accept/reject decision, so the gate-ROC / gate-AUC columns — this
   benchmark's one metric no competitor reports — are **unavailable** for
   any method scored via the external-transform path at the mid rung: the
   published manifest carries no per-tile accept/reject data for the scorer
   to reconstruct against, because that data lives only in
   `TILED_REG_TILE`'s (unpublished) output.

**This gap is not merely "the columns come out blank."** When the metric
functions in `gate_roc.py` are fed the empty `accepted`/`scores` that the
external-transform path produces, the arithmetic does not degrade to
missing values — it produces numbers that read as a real result:
`gate_recall = 0 / (0 + fn)` evaluates to a literal **`0.0`** for any pair
with at least one truly-registrable tile (every real pair), and `gate_auc`
evaluates to the documented tie-broken **`0.5`** for an uninformative
constant score. Only `gate_precision` (`0/0`) comes out genuinely `NaN`,
and `intrinsic_tre` genuinely `None`. Written into a paper-facing table
captioned "recall of the registration-acceptance gate against ground
truth," `0.0` recall and `0.5` AUC for every method is a fabricated finding
— it says "every method's gate has zero recall and no discriminating
power," which was never measured. A fix in `cli.py` to make these columns
genuinely `None` (rather than arithmetically-derived zeros) when no
per-tile data is available was in flight in this worktree as this document
was written.

A pipeline fix publishing the missing control-point JSONs (under
`<outdir>/<pid>/registered/controls/*_ctrl.json`, a sub-path of the
existing `registered` `PUBLISHED_KIND`, following `TILED_SOLVE`'s existing
`manifest/` sub-path precedent) has landed on `feat/stare-ultimate`
(commit `6823d4b`), but **not yet merged into this branch (`bench/
stare-synthetic`) or into `dev`**. Until that merge happens, the gate-ROC
family is unavailable — not zero, not blank, genuinely absent — for every
method scored via an externally supplied transform, and the VALIS row
cannot be produced at all: `benchmarks/stare_bench/cli.py`'s `score_pair`
raises rather than fabricating a score when no transform is discoverable
(Ruling 40).

---

## 3. PENDING — not measured, explicitly outstanding

**None of the following numbers exist yet. They are not placeholders for
numbers that were "basically" measured — no run has been attempted, because
this environment has no cluster and no pulled container images.**

- **Mid rung (528-row sweep, 20480² images, `run_mid.sh`).** The runner is
  built and reviewed (`benchmarks/tests/stare_bench/test_run_mid.py`), and a
  512x512 stand-in generation smoke-test confirmed correct `truth.json` and
  `samplesheet.csv` output, but Nextflow itself has **never been executed**
  against the real sweep. Zero of the 528 rows have been scored.
- **Gigapixel rung.** Not attempted at any scale. No timing, no memory
  measurement, no output exists.
- **The ≤8 GB peak-RSS claim.** `metrics/cost.py`'s `measure()` (self +
  waited-for children RSS) and `from_trace()` (Nextflow trace parsing) are
  implemented and unit-tested, but **no gigapixel-scale process has ever been
  measured with them**. There is no number to report — not "under 8 GB", not
  "close to 8 GB", nothing. This is the headline empirical claim the
  benchmark was built to test, and it is the most important line in this
  document: **it is outstanding.**
- **Any competitor score.** VALIS and ASHLAR have never been run through
  this harness at any rung above unit-scale wiring checks; the mid rung
  cannot score VALIS at all until Finding 2's publishing gap is fixed.
- **Gate-ROC / gate-AUC at the mid or gigapixel rung.** Computable in
  principle from the metrics module, but the input (STARE's per-tile
  control-point JSONs) is not published at those rungs yet (Finding 2).

Reporting any of the above as measured before an actual run — including
rounding an unrun number to "≈8 GB" or otherwise implying it was observed —
would misrepresent an unrun experiment as a citable result. Do not do that
with this document.

---

## 4. Test count and lint at freeze

At the time this record was written: `python -m pytest
benchmarks/tests/stare_bench -v` — **135 tests total, 135 passing, no known
failures.** An earlier full-suite run surfaced one intermittent failure in
`test_cost.py::test_measure_reports_subprocess_children_not_just_self`; the
root cause was not system load but a structural defect in the test itself —
`ru_maxrss` is a lifetime high-water mark that never resets within a
process, so once the pytest process had already peaked above the spawned
child's allocation, the asserted delta collapsed to zero. The test now runs
its comparison in a fresh interpreter and was verified passing under three
conditions: isolated (7/7), natural full-suite order (135/135), and a forced
adversarial ordering with the memory-heavy tests first (32/32). `ruff check
--no-force-exclude benchmarks/stare_bench benchmarks/tests/stare_bench`
reports no violations.
