"""N4 bias-field illumination correction (optional, via SimpleITK).

N4 (Tustison et al., IEEE TMI 2010) estimates a smooth, low-frequency MULTIPLICATIVE
bias field by iteratively fitting a B-spline surface to the log-intensity histogram-
sharpened image. It is a parametric single-image shading model — a different family
from this harness's periodic per-tile flat-field (which exploits the mosaic's tile
periodicity) and from BaSiC's low-rank+sparse multi-tile decomposition. Included as a
benchmark contender per the SOTA review (research/illumination-correction-sota-
2026-07-22.md): "successfully applied as flat-field correction in microscopy," though
situational vs BaSiC for mIF.

SimpleITK is an OPTIONAL dependency (mirrors basicpy / scikit-image here): if it is not
installed, `n4_correct` raises ImportError and the benchmark driver skips the `n4_*`
variants, exactly as it skips `baseline-basic` without basicpy.
"""
from __future__ import annotations
import numpy as np


def n4_correct(image, out_dtype=np.uint16, shrink=4, iterations=(50, 50, 50, 50),
               fwhm=0.15) -> np.ndarray:
    """Divide out an N4-estimated smooth multiplicative bias field.

    Parameters
    ----------
    image : 2-D array (any numeric dtype; float or integer)
    out_dtype : integer dtype to clip/cast the corrected image back to.
    shrink : downsample factor for the bias-field ESTIMATION only (speed on large
        mosaics); the field is upsampled to full resolution before dividing, so
        output resolution is unchanged. N4's field is smooth, so this is ~lossless.
    iterations : max fitting iterations per multi-resolution level.
    fwhm : bias-field FWHM (control-point spacing prior); larger = smoother field.

    Raises
    ------
    ImportError : if SimpleITK is not installed (driver then skips the variant).
    """
    try:
        import SimpleITK as sitk
    except Exception as e:   # ImportError or a broken install
        raise ImportError("N4 flat-field requires SimpleITK; pip install SimpleITK") from e

    f = np.ascontiguousarray(image, dtype=np.float32)
    img = sitk.GetImageFromArray(f)
    # N4 needs a strictly positive image (it works in log space); offset a hair so
    # zero/background pixels don't produce -inf.
    img = sitk.Cast(img, sitk.sitkFloat32) + 1.0

    # Estimate on a shrunk image for speed, then apply the full-resolution field.
    shrink = max(int(shrink), 1)
    small = sitk.Shrink(img, [shrink] * img.GetDimension()) if shrink > 1 else img
    mask = sitk.OtsuThreshold(small, 0, 1, 200)   # fit over foreground+background robustly

    corrector = sitk.N4BiasFieldCorrectionImageFilter()
    corrector.SetMaximumNumberOfIterations(list(iterations))
    corrector.SetBiasFieldFullWidthAtHalfMaximum(float(fwhm))
    corrector.Execute(small, mask)

    # Reconstruct the full-resolution log-bias field, exponentiate, divide it out.
    log_bias = corrector.GetLogBiasFieldAsImage(img)         # full-res field
    corrected = img / sitk.Exp(log_bias)

    out = sitk.GetArrayFromImage(corrected).astype(np.float32) - 1.0
    info = np.iinfo(out_dtype)
    return np.clip(out, info.min, info.max).astype(out_dtype)
