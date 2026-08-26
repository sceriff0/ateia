"""Microscopy realism layers, each reporting exactly what it degraded.

THE RETURNED RETENTION MAP IS THE POINT. Every layer returns
``(image, retained)`` where ``retained[y, x]`` is the fraction of pre-layer
signal that survived at that pixel. Composed multiplicatively across layers,
that map is what lets labels.py decide which tiles were TRULY registrable --
computed from the generator's own state, never from any method's output. A
label derived from a method's behaviour would reintroduce the circularity the
whole benchmark exists to avoid.

The bleaching and blank-region layers matter most: together they reproduce the
40-90%-blank band that this repo's own dense sweep found no per-tile threshold
can separate.
"""

from __future__ import annotations

import numpy as np

__all__ = ["photobleach", "blank_regions", "noise_and_psf", "background", "apply_all"]

# Layer order is fixed and meaningful: signal is bleached, then destroyed, then
# imaged (PSF + noise), then contaminated by background. Reordering changes the
# physics -- e.g. blurring before blanking would smear signal into blank regions.
LAYER_ORDER = ("photobleach", "blank_regions", "noise_and_psf", "background")


def photobleach(img, rng, factor):
    """Uniform inter-cycle intensity decay. ``factor`` in (0, 1]; 1.0 = no bleach."""
    factor = float(factor)
    if not 0.0 < factor <= 1.0:
        raise ValueError(f"photobleach factor must be in (0, 1], got {factor}")
    retained = np.full(img.shape, factor, dtype=np.float32)
    return (img * factor).astype(np.float32), retained


def blank_regions(img, rng, fraction, n_blobs=6):
    """Zero out contiguous blobs until ``fraction`` of the frame is blank.

    Blobs, not scattered pixels: a torn or missing region of tissue is
    contiguous, and contiguity is what defeats a per-tile threshold.
    """
    fraction = float(fraction)
    if not 0.0 <= fraction <= 1.0:
        raise ValueError(f"blank fraction must be in [0, 1], got {fraction}")
    h, w = img.shape
    retained = np.ones((h, w), dtype=np.float32)
    if fraction <= 0.0:
        return img.astype(np.float32), retained
    if fraction >= 1.0:
        return np.zeros_like(img, dtype=np.float32), np.zeros((h, w), dtype=np.float32)

    ys, xs = np.mgrid[0:h, 0:w]
    target = fraction * h * w
    # Start from the area each blob would need if they did not overlap, then
    # grow until the realised blank area reaches the target.
    radius = np.sqrt(target / (n_blobs * np.pi))
    for _ in range(60):
        mask = np.zeros((h, w), dtype=bool)
        local = np.random.default_rng(rng.integers(0, 2**31 - 1))
        for _ in range(n_blobs):
            cy = local.uniform(0, h)
            cx = local.uniform(0, w)
            mask |= ((ys - cy) ** 2 + (xs - cx) ** 2) <= radius**2
        got = mask.mean()
        if abs(got - fraction) < 0.02:
            break
        radius *= np.sqrt(fraction / got) if got > 0 else 1.5

    retained[mask] = 0.0
    out = img.astype(np.float32).copy()
    out[mask] = 0.0
    return out, retained


def noise_and_psf(img, rng, psf_sigma_px, photons, read_sigma):
    """Gaussian PSF blur, then Poisson shot noise, then Gaussian read noise."""
    from scipy.ndimage import gaussian_filter

    blurred = gaussian_filter(img.astype(np.float32), float(psf_sigma_px))
    photons = float(photons)
    if photons > 0:
        noisy = rng.poisson(np.clip(blurred, 0, None) * photons) / photons
    else:
        noisy = blurred
    noisy = noisy + rng.normal(0.0, float(read_sigma), size=img.shape)
    out = np.clip(noisy, 0.0, None).astype(np.float32)
    # Blur redistributes signal but does not destroy it; noise is additive.
    # Neither empties a pixel, so retention is unchanged here.
    return out, np.ones(img.shape, dtype=np.float32)


def background(img, rng, af_amplitude, seam_px, seam_amplitude, focus_sigma_px):
    """Autofluorescence gradient, acquisition-tile seams, and focus drift."""
    from scipy.ndimage import gaussian_filter

    h, w = img.shape
    ys, xs = np.mgrid[0:h, 0:w]
    ang = rng.uniform(0, 2 * np.pi)
    ramp = (np.cos(ang) * xs / max(w - 1, 1) + np.sin(ang) * ys / max(h, 1))
    af = float(af_amplitude) * (ramp - ramp.min()) / max(np.ptp(ramp), 1e-9)

    seams = np.zeros((h, w), dtype=np.float32)
    if seam_px and seam_amplitude:
        step = int(seam_px)
        tile_gain = rng.normal(0.0, float(seam_amplitude),
                               size=(h // step + 1, w // step + 1))
        seams = np.repeat(np.repeat(tile_gain, step, axis=0), step, axis=1)[:h, :w]

    out = img.astype(np.float32) + af.astype(np.float32) + seams
    if focus_sigma_px:
        out = gaussian_filter(out, float(focus_sigma_px))
    out = np.clip(out, 0.0, None).astype(np.float32)
    return out, np.ones(img.shape, dtype=np.float32)


_LAYERS = {
    "photobleach": photobleach,
    "blank_regions": blank_regions,
    "noise_and_psf": noise_and_psf,
    "background": background,
}


def apply_all(img, params, rng):
    """Apply the configured layers in LAYER_ORDER, composing retention."""
    out = np.asarray(img, dtype=np.float32)
    retained = np.ones(out.shape, dtype=np.float32)
    for name in LAYER_ORDER:
        if name not in params:
            continue
        out, layer_retained = _LAYERS[name](out, rng, **params[name])
        retained = retained * layer_retained
    return out, np.clip(retained, 0.0, 1.0).astype(np.float32)
