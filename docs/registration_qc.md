# Staged registration QC (`reg_qc = 2`)

At `reg_qc = 2` the pipeline answers a question the DAPI overlay cannot: **which registration
stage actually improved the alignment, and by how much?**

It does that by segmenting nuclei on each slide's native image, establishing cell-to-cell
correspondence **once**, and then re-measuring those same cell pairs after each stage of the
VALIS transform.

## Why the correspondence is fixed, and fixed *there*

VALIS applies four transform states in this order:

| Stage | What it is | Where it comes from |
|---|---|---|
| `native` | no transform | the segmentation as written |
| `rigid` | the rigid transform, after `MicroRigidRegistrar` refined it | the registrar pickle, `non_rigid=False` |
| `non_rigid` | rigid + the wave-1 non-rigid displacement field | the REGISTER **stage checkpoint** |
| `micro` | rigid + non-rigid + the micro-registration residual | the registrar pickle, `non_rigid=True` |

`rigid` is the anchor. Two reasons:

1. **Before registration the cells are too far apart to pair.** Two rounds of a cyclic-IF
   experiment sit a whole tissue-offset apart in their native frames; any matcher run there is
   pairing noise, not cells.
2. **`rigid` is the transform every later stage builds on.** `MicroRigidRegistrar` runs *inside*
   `Valis.register()`, before the non-rigid stage, so the rigid transform in the final pickle is
   already the one the non-rigid and micro fields were fitted on top of. A pairing valid at
   `rigid` is therefore valid at every later stage by construction.

Holding the pairing is the point. If the match were re-derived per stage, a score could move
because *different cells got paired* — or because a cell that was occluded in one stage's raster
reappeared in another's. Neither is a registration effect. With the pairing fixed, every change
between stages is geometry.

Pairing rule: **mutual nearest centroid within one median nuclear radius**. Mutuality stops a
dense cluster collapsing onto one popular target; the radius stops a cell with no true partner
pairing with whatever happens to be nearest. Both are tunable
(`--match-radius-factor`, `--match-radius-px`).

## Metrics

Per stage, over the fixed pairs:

- **Per-pair IoU** — `iou_mean`, `iou_p10/p50/p90`, `iou_max`, and `frac_iou_ge_0.5`.
  Each pair is rasterized in its own bounding-box window (supersampled 2× by default), so peak
  memory is one nucleus rather than one slide.
- **Centroid residual displacement** — `displacement_px_p50/p90/max`, plus `displacement_um_*`
  when the slide metadata carries a physical pixel size. This is usually the number to quote: it
  has no resolution floor the way a thresholded IoU does.
- **`dice_matched`** — areal Dice restricted to the matched pairs. Named to make clear it is not
  a whole-slide foreground Dice; unmatched cells contribute nothing.

`delta_vs_anchor` reports each stage minus `rigid`. **A positive displacement delta means that
stage made the alignment worse** — which is exactly the failure this design exists to surface,
since micro-registration is caught-and-continued in `bin/register.py` and would otherwise fail
silently.

## The REGISTER stage checkpoint

VALIS composes its stages destructively:

- `MicroRigidRegistrar` overwrites `slide.M`.
- `register_micro()` does `fwd_dxdy = fwd_dxdy + micro_residual` and writes it back onto the
  same attribute.

So a finished registrar pickle holds **one** field that is rigid + non-rigid + micro, and no
reader can recover the intermediate. REGISTER therefore snapshots each slide's forward
displacement field at the only moment it exists — after `register()` returns, before
`register_micro()` is called — into `reg_stage_checkpoint/`. Fields are at non-rigid
registration resolution (a few thousand pixels a side), not slide resolution: tens of MB.

Writing it can never fail a registration; it is QC input, and QC here is non-gating. When it is
missing, the QC still runs and reports `native`, `rigid` and `micro`, sets
`stages_separable: false`, and records a `note` saying why `non_rigid` is absent. When
micro-registration did not run (skipped, or it raised and was caught), the checkpoint records
that and the QC omits the `micro` stage rather than reporting a byte-for-byte duplicate of
`non_rigid`.

## Output

`<outdir>/<patient>/qc/registration/<patient>_<slide>_seg_qc.json`:

```json
{
  "patient_id": "P001",
  "moving": "P001_cycle2",
  "reference": "P001_cycle1",
  "stages_separable": true,
  "stage_order": ["native", "rigid", "non_rigid", "micro"],
  "matching": {
    "method": "mutual_nn_centroid",
    "anchor_stage": "rigid",
    "radius_px": 6.2,
    "median_cell_radius_px": 6.2,
    "n_pairs": 184203,
    "pair_fraction": 0.91
  },
  "stages": {
    "rigid":     { "n_pairs": 184203, "iou_mean": 0.42, "displacement_px_p50": 4.1, "displacement_um_p50": 1.33 },
    "non_rigid": { "n_pairs": 184203, "iou_mean": 0.71, "displacement_px_p50": 1.6, "displacement_um_p50": 0.52 },
    "micro":     { "n_pairs": 184203, "iou_mean": 0.78, "displacement_px_p50": 1.1, "displacement_um_p50": 0.36 }
  },
  "delta_vs_anchor": {
    "micro": { "iou_mean": 0.36, "displacement_px_p50": -3.0 }
  },
  "counts": { "features_ref": 201884, "features_moving": 198110 }
}
```

Read it as: rigid alone left a median residual of 4.1 px (1.33 µm); non-rigid took that to
1.6 µm-worth; micro-registration bought a further 0.5 px. `pair_fraction` below ~0.5 means the
pairing itself is thin — check the rigid stage before trusting the later numbers.

## Reading the numbers critically

- **`pair_fraction`** is the first thing to check. A low value means rigid registration left the
  slides too far apart for a nuclear-radius match, so every later stage is being measured on a
  biased subset of cells that happened to land close.
- **`n_pairs_scored` vs `n_pairs`** — the difference is pairs whose bounding box exceeded
  `--max-pair-window-px` (a warp artifact or merged blob). A large gap means distorted geometry,
  not a bad alignment.
- **IoU is bounded by segmentation agreement, not just registration.** Two independent StarDist
  runs on the same nucleus in different rounds do not produce identical outlines, so `iou_mean`
  has a ceiling below 1 even under perfect registration. The *delta* between stages is the
  registration signal; the absolute level is not.
- **Vertices are not clipped to the aligned frame by default**, unlike `Slide.warp_geojson`.
  Clipping flattens boundary-straddling cells onto the crop edge in both slides, inflating their
  IoU for reasons unrelated to registration. Pass `--clip-to-frame` to reproduce VALIS exactly.
