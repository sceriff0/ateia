# Illumination correction & its validation for tiled fluorescence WSI — SOTA research

**Date:** 2026-07-22
**Context:** grounding the redesign of `bin/illum/` benchmark metrics so the leaderboard
cannot be gamed by over-subtraction (the current top variant wins by driving the image toward black).

## Executive summary

The field-standard correction is **BaSiC / BaSiCPy** (low-rank+sparse flatfield + optional
darkfield), applied **per-tile before stitching** (MCMICRO estimates profiles, ASHLAR applies them
at stitch time). Correcting an already-stitched mosaic — what this harness does — is a known
compromise that bakes seams in, so a *periodic* mosaic model is a reasonable but second-best path.
The decisive research result for us is on **validation**: a background-flatness metric like CV is
**trivially gamed by zeroing the image**, and the fix is a **fidelity gate** — pair every
artifact-suppression term with a signal-preservation term (SSIM against a ground-truth phantom;
retained dynamic range / foreground correlation on real data) and **multiply** them so a destroyed
image scores ~0 regardless of how "flat" it looks. Bound every term to [0,1]. Validate weights, and
ideally the whole ranking, against a **downstream proxy** (cross-tile intensity uniformity, cell
counts), because generic image-quality metrics can diverge from biological accuracy.

## 1. Correction methods

- **BaSiC** — Peng et al., *Nat. Commun.* 8:14836 (2017), [10.1038/ncomms14836](https://www.nature.com/articles/ncomms14836).
  Low-rank + sparse decomposition → smooth multiplicative flatfield + additive darkfield from a stack
  of tiles; no reference images, no manual params; models temporal baseline drift. De-facto default for mIF.
- **BaSiCPy** — [peng-lab/BaSiCPy](https://github.com/peng-lab/BaSiCPy) (~105★, MIT, v2.0.0 Mar 2026),
  JAX reimplementation. ⚠️ Documented instability: `Reweighting did not converge` → NaN darkfield /
  zero flatfield on low-tile-count or low-diversity stacks ([issue #104](https://github.com/peng-lab/BaSiCPy/issues/104));
  darkfield **over-corrects** dim regions. Docs: `get_darkfield=False` by default, enable only for a
  genuine additive offset; raise `smoothness_*` for smoother profiles; needs a diverse tile set.
- **CIDRE** — Smith et al., *Nat. Methods* 12:404 (2015), [10.1038/nmeth.3323](https://www.nature.com/articles/nmeth.3323).
  Earlier retrospective energy-minimization; largely superseded by BaSiC.
- **Background subtraction** (rolling-ball / white-tophat / gaussian, [ImageJ](https://imagej.net/plugins/rolling-ball-background-subtraction),
  [skimage.restoration](https://scikit-image.org/docs/stable/api/skimage.restoration.html)):
  per-image **additive** background removal only — NOT a substitute for multiplicative vignetting.
  Rolling-ball radius must exceed the largest real object; noise-sensitive.
- **N4/N3 bias field** (ITK/SimpleITK): single-image low-frequency bias; rarely used in mIF vs BaSiC.
- **Deep learning**: SSCOR stripe self-correction, *Nat. Commun.* 14:5106 (2023),
  [10.1038/s41467-023-41165-1](https://www.nature.com/articles/s41467-023-41165-1) — targets stripes, niche vs BaSiC.
- **Pipelines**: MCMICRO (*Nat. Methods* 2022, [10.1038/s41592-021-01308-y](https://www.nature.com/articles/s41592-021-01308-y))
  runs BaSiC per-tile; ASHLAR (*Bioinformatics* 2022, [10.1093/bioinformatics/btac544](https://academic.oup.com/bioinformatics/article/38/19/4613/6668278))
  applies FFP/DFP at stitch time, no built-in estimation.

**Best practice / failure modes:** correct tiles *before* stitching; BaSiC needs a diverse stack;
enable darkfield only when a real additive offset exists (else it over-corrects); reject NaN/zero/
non-converged BaSiC outputs; rolling-ball is additive cleanup, never multiplicative shading.

## 2. Validation / quality metrics (the important part)

**Seam / tile-periodicity**
- Normalized **FFT power at the tile-pitch frequency** is standard (correct images show no peak at the
  grid fundamental; Chang et al., *GigaScience* 2020, [10.1093/gigascience/giaa035](https://academic.oup.com/gigascience/article/9/4/giaa035/5819874)).
  ⚠️ sensitive to windowing/DC leakage and real periodic biology — use a **narrow band around the known
  pitch, normalized to broadband power**.
- **Overlap-region mean-abs-difference** (BaSiC "correction score" Γ′, Peng 2017) is the most-trusted,
  hard-to-game seam metric: intensity discontinuity in tile overlaps, normalized by the pre-correction value.
- No-reference seam: optical-flow displacement across seams (vEMstitch, *GigaScience* 2024,
  [10.1093/gigascience/giae076](https://academic.oup.com/gigascience/article/doi/10.1093/gigascience/giae076/7845194)); NCC/SSIM on overlaps (ITKMontage).

**Background flatness — the CV trap**
- ⚠️ **CV = std/mean of a background region is trivially minimized by subtracting/zeroing** (mean→0
  inflates/NaNs it; a global offset lowers it without improving flatness). CV alone rewards over-correction.
- Define "background" from a **fixed reference ROI / empty field**, NOT a percentile threshold on the
  *corrected* image (circular — the threshold moves with the correction you are scoring). Singh et al.
  *J. Microscopy* 2014 [10.1111/jmi.12178](https://onlinelibrary.wiley.com/doi/full/10.1111/jmi.12178);
  Model *Cytometry* 2001. Report **preserved mean intensity** alongside CV.
- **QUAREP-LiMi WG3** field-flatness/illumination-uniformity is the QC standard (Nelson et al.,
  *J. Microscopy* 2021, [10.1111/jmi.13041](https://onlinelibrary.wiley.com/doi/full/10.1111/jmi.13041);
  consortium paper *Nat. Methods* 2021, [10.1038/s41592-021-01162-y](https://www.nature.com/articles/s41592-021-01162-y)) —
  max-min/CV on a uniform dye reference (offset-invariant complement).
- **EVEN** (Babaei et al., *Nat. Commun.* 2025, [10.1038/s41467-025-68150-0](https://www.nature.com/articles/s41467-025-68150-0)) evaluates flat-field correction quality directly.

**Signal preservation / fidelity**
- **SSIM** — Wang et al., *IEEE TIP* 13(4):600 (2004), [10.1109/TIP.2003.819861](https://doi.org/10.1109/TIP.2003.819861):
  SSIM = (2μₓμᵧ+C₁)(2σₓᵧ+C₂)/[(μₓ²+μᵧ²+C₁)(σₓ²+σᵧ²+C₂)]. Against a clean phantom, a zeroed image sends
  μᵧ→0 and collapses the score — **best single anti-gaming term**. **MS-SSIM** (Asilomar 2003) is the multiscale variant.
- **PSNR/RMSE**: cheap; compute corrected-vs-clean AND recovered-flatfield-vs-injected-flatfield error. ⚠️ weak perceptually alone.
- **FSIM** (Zhang et al., *IEEE TIP* 2011): phase-congruency + gradient; rewards structure retention.
- **Retention (no-reference)**: retained 99th-percentile foreground intensity, dynamic-range ratio,
  Pearson/mutual-information between corrected and original **inside a foreground mask** — all collapse for a subtracted image.

**Anti-gaming composite**
- **Perception-distortion tradeoff** (Blau & Michaeli, *CVPR* 2018): no single metric captures both →
  pair fidelity + artifact terms.
- ⚠️ **IQMs can diverge from biological information** (bioRxiv 2025, [2025.08.05.668508](https://www.biorxiv.org/content/10.1101/2025.08.05.668508)) → validate against a task.
- Recipe: (1) fidelity as a **multiplicative gate**, `score = artifact_gains × SSIM^α` (or retention on
  real data) — black image → SSIM→0 → product→0; (2) **retention floor** hard-fail; (3) **bound every
  term to [0,1]** (`g/(1+g)` or clip); (4) on the phantom, add recovered-flatfield RMSE; (5) tune weights against a downstream task.

## 3. Downstream validation (ultimate ground truth for mIF)

- KASK et al., *J. Microscopy* 2016 ([10.1111/jmi.12404](https://onlinelibrary.wiley.com/doi/10.1111/jmi.12404)):
  shading impact on quantification is "severe"; flatfield correction equalizes cross-tile intensity,
  makes global thresholding valid, increases detected-cell counts.
- Harkin et al. slide-to-slide variation ([PMC8896603](https://pmc.ncbi.nlm.nih.gov/articles/PMC8896603/));
  QUAL-IF-AI artifact QC (bioRxiv 2024, [2024.01.26.577391](https://www.biorxiv.org/content/10.1101/2024.01.26.577391v1.full)).
- Practical downstream metrics: **cross-tile marker-intensity uniformity/CV, detected-cell counts,
  threshold portability**. Rigorous proof that correction improves the *biological* readout is sparse —
  so measure your own before/after cross-tile CV and cell counts rather than assuming.

## Concrete recommendations for this harness

1. **Keep** normalized-FFT seam (narrow band / broadband), **add** an overlap/boundary discontinuity seam metric.
2. **Fix** background flatness: fixed ROI from the *uncorrected* image; report preserved mean; do not
   threshold the corrected image.
3. **Add a fidelity gate**: no-reference retention (foreground correlation + retained dynamic range) on
   real data; full-reference **SSIM/RMSE vs the synthetic clean phantom** + recovered-flatfield RMSE in tests.
4. **Composite = bounded artifact score × fidelity** (multiplicative), every term in [0,1].
5. **Un-gameability regression test**: a degenerate zeroing variant must rank BELOW the ground-truth-correct
   variant under the new composite (it did not under seam+cv).
6. **Add a cross-tile-uniformity** (per-tile mean CV) diagnostic as a downstream proxy.
7. Drop rolling-ball as a serious candidate (additive-only, ~45× slower, negative score).

## Sources
See inline DOIs/links above. Papers: BaSiC (ncomms14836), CIDRE (nmeth.3323), MCMICRO (s41592-021-01308-y),
ASHLAR (btac544), SSIM (TIP 2003.819861), MS-SSIM (Asilomar 2003), FSIM (TIP 2011.2109730),
Blau-Michaeli (CVPR 2018), QUAREP-LiMi (s41592-021-01162-y, jmi.13041), EVEN (s41467-025-68150-0),
KASK (jmi.12404), Chang (giaa035), SSCOR (s41467-023-41165-1).
Repos: peng-lab/BaSiCPy, labsyspharm/mcmicro, labsyspharm/ashlar (all MIT, maintained).
