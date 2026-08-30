"""Global rigid anchor (M0) estimation for the STARE registration method.

The COARSE step aligns a whole moving slide to the reference with a single affine ``M0``, cheaply,
on downsampled images. Feature matching (ORB) handles the inter-cycle rotation/translation that a
translation-only phase correlation could not; the per-tile step later refines the small residual.

The numerically load-bearing piece is :func:`estimate_transform_from_matches` — given point
correspondences it solves for the transform and reports a residual Target Registration Error. It
is deterministic and unit-tested. :func:`estimate_rigid` wraps it with the ORB feature front-end.

Pure NumPy + scikit-image (no VALIS, no JVM).
"""

from __future__ import annotations

import time

import numpy as np
from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "estimate_transform_from_matches",
    "estimate_rigid",
    "estimate_affine",
    "FRONTENDS",
    "normalize_for_orb",
    "scale_transform_to_full_res",
]

_MIN_SAMPLES = {"euclidean": 2, "similarity": 2, "affine": 3}

# The four selectable COARSE global-alignment front-ends. "orb" is the default and must stay
# behaviour-preserving with every run before this change. "sift" and "fourier_mellin" are
# zero-new-dependency CPU alternatives (skimage ships both already); "disk_lightglue" is the
# learned matcher VALIS/ACROBAT winners use and needs torch+kornia, which the `:tiled` image
# now carries.
FRONTENDS = ("orb", "sift", "fourier_mellin", "disk_lightglue")


def _model_class(model):
    from skimage.transform import (
        AffineTransform,
        EuclideanTransform,
        SimilarityTransform,
    )

    return {
        "euclidean": EuclideanTransform,
        "similarity": SimilarityTransform,
        "affine": AffineTransform,
    }[model]


def _ransac(data, cls, min_samples, residual_threshold, max_trials, random_state):
    """Call skimage.measure.ransac across the rng/random_state kwarg rename."""
    from skimage.measure import ransac

    kw = dict(
        min_samples=min_samples,
        residual_threshold=residual_threshold,
        max_trials=max_trials,
    )
    try:
        return ransac(data, cls, rng=random_state, **kw)
    except TypeError:
        return ransac(data, cls, random_state=random_state, **kw)


def _rms(residuals_xy):
    if len(residuals_xy) == 0:
        return float("nan")
    return float(np.sqrt(np.mean(np.sum(np.asarray(residuals_xy) ** 2, axis=1))))


def estimate_transform_from_matches(
    src,
    dst,
    model="euclidean",
    robust=True,
    residual_threshold=3.0,
    max_trials=1000,
    random_state=0,
):
    """Estimate the transform mapping ``src`` -> ``dst`` and its residual TRE.

    Returns ``(M, residual_px, n_inliers)`` where ``M`` is the 3x3 forward affine, ``residual_px``
    is the RMS distance between the transformed inlier ``src`` and ``dst`` (the fit's Target
    Registration Error), and ``n_inliers`` is how many correspondences the fit used.
    """
    src = np.asarray(src, dtype=float)
    dst = np.asarray(dst, dtype=float)
    if model not in _MIN_SAMPLES:
        raise ValueError(
            f"unknown model {model!r}; expected one of {list(_MIN_SAMPLES)}"
        )

    cls = _model_class(model)
    if robust and len(src) > _MIN_SAMPLES[model]:
        tform, inliers = _ransac(
            (src, dst),
            cls,
            _MIN_SAMPLES[model],
            residual_threshold,
            max_trials,
            random_state,
        )
        if tform is None:  # ransac failed to find a consensus set
            inliers = np.ones(len(src), dtype=bool)
            tform = cls()
            tform.estimate(src, dst)
    else:
        inliers = np.ones(len(src), dtype=bool)
        tform = cls()
        tform.estimate(src, dst)

    m = np.asarray(tform.params, dtype=float)
    residual = _rms(tform(src[inliers]) - dst[inliers])
    return m, residual, int(np.count_nonzero(inliers))


def scale_transform_to_full_res(m, factor):
    """Lift a transform estimated on ``factor``-decimated images back to full-resolution pixels.

    A decimated coordinate relates to the full-resolution one by ``p_ds = p_full / factor``, so
    the full-resolution map is ``diag(f, f, 1) @ M_ds @ diag(1/f, 1/f, 1)``. The two scalings
    cancel across the linear 2x2 block and survive only on the translation column -- i.e. the
    rotation/scale part is invariant and only the offsets grow by ``factor``. Returning ``M_ds``
    unchanged is the natural mistake, and it under-translates by exactly ``factor``.
    """
    m = np.array(m, dtype=float, copy=True)
    factor = float(factor)
    m[:2, 2] *= factor
    return m


# ORB's `fast_threshold` is an ABSOLUTE intensity difference, and skimage's `img_as_float`
# rescales only INTEGER inputs -- a float array passes through with its scale untouched. So the
# threshold is only meaningful on data already in [0, 1]. The planes reaching this module are raw
# microscopy counts (uint16, 0..65535) widened to float32 by `tiled_io.read_decimated`, where 0.05
# sits ~6 orders of magnitude below the data range: FAST then fires on ~38% of ALL pixels instead
# of ~3%, and `corner_peaks`' spacing pass -- quadratic in the candidate count -- turns a ~1 min
# anchor into a ~5 h one, past TILED_COARSE's 2 h walltime. Measured on a 2048^2 thumbnail:
# 1.1 s normalised vs 319 s raw; the gap widens with area (~x15.7 per x4 px).
#
# Normalising is also a CORRECTNESS fix, not only a speed one: reference and moving come from
# different imaging cycles with different exposures, so one absolute threshold applied to two
# different dynamic ranges can yield hundreds of keypoints on one slide and almost none on the
# other -- a starved or bogus M0, with no error raised.
#
# A percentile window, not `img / img.max()`: one hot pixel (routine in fluorescence) would
# otherwise compress the whole tissue into the bottom of the range and starve ORB of corners.
# p99.9 rather than p99 because DAPI nuclei ARE the bright minority -- clipping at p99 saturates
# the very structure the anchor matches on.
_ORB_PCT = (1.0, 99.9)


def normalize_for_orb(img):
    """Rescale ``img`` to [0, 1] over a robust percentile window (see the note above)."""
    a = np.asarray(img, dtype=float)
    lo, hi = (float(v) for v in np.percentile(a, _ORB_PCT))
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        lo, hi = float(np.nanmin(a)), float(np.nanmax(a))
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        return np.zeros_like(a)  # constant/all-NaN plane: no corners to find
    return np.clip((a - lo) / (hi - lo), 0.0, 1.0)


def _orb_features(img, n_keypoints, fast_threshold):
    from skimage.feature import ORB

    orb = ORB(n_keypoints=n_keypoints, fast_threshold=fast_threshold)
    orb.detect_and_extract(normalize_for_orb(img))
    # ORB keypoints are (row, col); the rest of the pipeline is (x, y)
    return orb.keypoints[:, ::-1], orb.descriptors


def _frontend_orb(
    ref,
    mov,
    model="euclidean",
    n_keypoints=800,
    fast_threshold=0.05,
    **kwargs,
):
    """ORB + RANSAC feature matching -- today's default COARSE front-end, extracted unchanged.

    Returns ``(M0, info)`` where ``info`` carries ``residual_px`` and ``n_inliers`` from
    :func:`estimate_transform_from_matches`. :func:`estimate_rigid` is the pre-existing,
    still-supported entry point and is now a thin reshape over this function.

    Logs each stage as it completes. ORB dominates this step's runtime and its cost is highly
    non-linear in thumbnail size, so a silent multi-hour stage here is indistinguishable from a
    hang -- which is exactly how the un-normalised-input regression above presented.
    """
    from skimage.feature import match_descriptors

    t0 = time.perf_counter()
    kp_ref, d_ref = _orb_features(ref, n_keypoints, fast_threshold)
    t_ref = time.perf_counter() - t0
    logger.info(
        f"coarse: ORB reference -> {len(kp_ref)} keypoints in {t_ref:.1f}s "
        f"({ref.shape[1]}x{ref.shape[0]})"
    )

    t1 = time.perf_counter()
    kp_mov, d_mov = _orb_features(mov, n_keypoints, fast_threshold)
    t_mov = time.perf_counter() - t1
    logger.info(
        f"coarse: ORB moving    -> {len(kp_mov)} keypoints in {t_mov:.1f}s "
        f"({mov.shape[1]}x{mov.shape[0]})"
    )

    matches = match_descriptors(d_mov, d_ref, cross_check=True)
    logger.info(
        f"coarse: {len(matches)} cross-checked matches -> {model} fit "
        f"(ORB total {t_ref + t_mov:.1f}s)"
    )
    if len(matches) < _MIN_SAMPLES[model]:
        # estimate_transform_from_matches would fall through to an unconstrained .estimate()
        # and hand back a silently meaningless M0; say so while the slide names are still in
        # the surrounding log context.
        logger.warning(
            f"coarse: only {len(matches)} matches for a {model} fit "
            f"(needs >= {_MIN_SAMPLES[model]}) -- M0 will be unreliable"
        )

    src = kp_mov[matches[:, 0]]
    dst = kp_ref[matches[:, 1]]
    m, residual, n_inliers = estimate_transform_from_matches(src, dst, model=model, **kwargs)
    return m, {"residual_px": residual, "n_inliers": n_inliers}


def estimate_rigid(
    ref,
    mov,
    model="euclidean",
    n_keypoints=800,
    fast_threshold=0.05,
    **kwargs,
):
    """Estimate ``M0`` mapping ``mov`` image coordinates onto ``ref`` via ORB feature matching.

    Returns ``(M0, residual_px, n_inliers)``. Pre-existing entry point, kept for backward
    compatibility with callers that predate the multi-front-end dispatch (:func:`estimate_affine`)
    -- it is a thin reshape over :func:`_frontend_orb`, which now holds the real implementation.
    """
    m, info = _frontend_orb(
        ref, mov, model=model, n_keypoints=n_keypoints, fast_threshold=fast_threshold, **kwargs
    )
    return m, info["residual_px"], info["n_inliers"]


def _frontend_sift(ref, mov, model="euclidean", **kwargs):
    """SIFT + RANSAC. Zero new dependency: skimage.feature.SIFT ships already.

    SIFT beats ORB on histology; ORB wins only on speed, and COARSE runs ONCE per slide, so
    speed is worth least exactly here.
    """
    from skimage.feature import SIFT, match_descriptors

    def _sift_features(img):
        sift = SIFT()
        sift.detect_and_extract(normalize_for_orb(img))
        # SIFT keypoints are (row, col), like ORB; the rest of the pipeline is (x, y)
        return sift.keypoints[:, ::-1], sift.descriptors

    t0 = time.perf_counter()
    kp_ref, d_ref = _sift_features(ref)
    t_ref = time.perf_counter() - t0
    logger.info(
        f"coarse: SIFT reference -> {len(kp_ref)} keypoints in {t_ref:.1f}s "
        f"({ref.shape[1]}x{ref.shape[0]})"
    )

    t1 = time.perf_counter()
    kp_mov, d_mov = _sift_features(mov)
    t_mov = time.perf_counter() - t1
    logger.info(
        f"coarse: SIFT moving    -> {len(kp_mov)} keypoints in {t_mov:.1f}s "
        f"({mov.shape[1]}x{mov.shape[0]})"
    )

    matches = match_descriptors(d_mov, d_ref, cross_check=True)
    logger.info(
        f"coarse: {len(matches)} cross-checked matches -> {model} fit "
        f"(SIFT total {t_ref + t_mov:.1f}s)"
    )
    if len(matches) < _MIN_SAMPLES[model]:
        logger.warning(
            f"coarse: only {len(matches)} matches for a {model} fit "
            f"(needs >= {_MIN_SAMPLES[model]}) -- M0 will be unreliable"
        )

    src = kp_mov[matches[:, 0]]
    dst = kp_ref[matches[:, 1]]
    m, residual, n_inliers = estimate_transform_from_matches(src, dst, model=model, **kwargs)
    return m, {"residual_px": residual, "n_inliers": n_inliers}


def _frontend_fourier_mellin(ref, mov, model="euclidean", **kwargs):
    """Log-polar Fourier-Mellin: keypoint-free rotation+scale+translation recovery.

    Uses skimage.transform.warp_polar plus the already-imported phase_cross_correlation. Zero
    new dependency, and it does not degrade on tissue too sparse to yield keypoints at all.

    Rotation/scale are recovered from the log-polar transform of each image's FFT magnitude
    spectrum -- the magnitude of a translated image's FFT is unchanged (the Fourier shift
    theorem), so that stage is translation-invariant and isolates rotation+scale. The residual
    translation is then recovered directly via phase cross-correlation between the images.

    NOTE: unlike the keypoint front-ends, there is no correspondence set here, so
    ``n_inliers`` is reported as the sentinel ``-1`` ("not applicable" -- there is no inlier
    count to report, and the sentinel is negative on purpose so it reads correctly against
    ``bin/tiled_coarse.py``'s ``n_inliers < 10`` low-inlier warning without tripping it on
    every run the way a hardcoded ``0`` used to). ``residual_px`` is unconditionally
    ``float("nan")``: phase_cross_correlation's normalised RMS error (~0-1) is not a
    pixel-distance RMS, so it is not comparable to the orb/sift residual or to
    ``reg_tiled_halo`` (a pixel threshold) -- reporting it as a pixel value silently inverted
    the halo-overrun warning in ``bin/tiled_coarse.py`` (a mis-registered slide could report a
    "residual" far below the halo and exit 0). NaN is honest and is already handled correctly
    by that warning's ``not np.isfinite`` branch.

    CAVEAT (unvalidated): the only test exercising this front-end
    (``tests/test_coarse_frontend.py::test_cpu_frontends_recover_a_pure_shift``) is a pure
    translation with zero rotation and unit scale, so the log-polar rotation/scale recovery
    code below executes but its correctness on an actual rotated/scaled pair is unverified.
    It also applies no window or bandpass filter before the FFT, so on real tissue (not this
    module's synthetic point fixture) edge leakage will likely dominate the rotation estimate.
    """
    from skimage.registration import phase_cross_correlation
    from skimage.transform import warp_polar

    r = normalize_for_orb(ref)
    m = normalize_for_orb(mov)
    if r.shape != m.shape:
        # log-polar rotation/scale recovery needs a common frame; crop to the shared extent
        h = min(r.shape[0], m.shape[0])
        w = min(r.shape[1], m.shape[1])
        r = r[:h, :w]
        m = m[:h, :w]

    r_fft_mag = np.abs(np.fft.fftshift(np.fft.fft2(r)))
    m_fft_mag = np.abs(np.fft.fftshift(np.fft.fft2(m)))
    radius = max(min(r_fft_mag.shape) // 2, 2)
    r_polar = warp_polar(r_fft_mag, radius=radius, scaling="log")
    m_polar = warp_polar(m_fft_mag, radius=radius, scaling="log")

    rs_shift, _rs_error, _ = phase_cross_correlation(r_polar, m_polar, upsample_factor=10)
    angle_bin, radius_bin = rs_shift
    n_angle = r_polar.shape[0]
    rotation_deg = (angle_bin / n_angle) * 360.0
    klog = radius / np.log(radius)
    scale = float(np.exp(radius_bin / klog))

    t_shift, _t_error, _ = phase_cross_correlation(r, m, upsample_factor=10)
    dy, dx = (float(v) for v in t_shift)
    logger.info(
        f"coarse: Fourier-Mellin rotation={rotation_deg:+.2f}deg scale={scale:.4f} "
        f"dx={dx:+.1f}px dy={dy:+.1f}px (log-polar {ref.shape[1]}x{ref.shape[0]})"
    )

    tform_kwargs = {"rotation": np.deg2rad(-rotation_deg), "translation": (dx, dy)}
    if model in ("similarity", "affine"):
        tform_kwargs["scale"] = 1.0 / scale if scale else 1.0
    tform = _model_class(model)(**tform_kwargs)
    m0 = np.asarray(tform.params, dtype=float)

    return m0, {"residual_px": float("nan"), "n_inliers": -1}


# DISK + LightGlue weights are ~52 MB and building both models dominates a single call.
# COARSE runs once per slide per task, so this cache only matters for the in-process
# oracle (bin/utils/tiled_pipeline.py) and the test suite -- but it is free there.
#
# The MODEL CLASSES are passed IN rather than imported here, and that is load-bearing:
# tests/test_container_harmonisation.py::
# test_tiled_container_torch_kornia_imports_are_confined_to_disk_lightglue asserts that
# every `import torch` / `from kornia...` in this module and in bin/tiled_coarse.py sits
# inside a function named literally `_frontend_disk_lightglue`. A `from kornia.feature
# import ...` in this helper is an offender and turns that guard RED.
_DISK_MODELS = {}


def _disk_models(device, disk_cls, lightglue_cls):
    key = str(device)
    if key not in _DISK_MODELS:
        _DISK_MODELS[key] = (
            disk_cls.from_pretrained("depth").to(device).eval(),
            lightglue_cls("disk").to(device).eval(),
        )
    return _DISK_MODELS[key]


def _to_disk_tensor(img, torch):
    """One plane -> the 1x3xHxW float32 batch DISK expects.

    Normalising is not optional for a LEARNED matcher: DISK was trained on images in
    [0, 1], and the planes reaching this module are raw microscopy counts widened to
    float32 (0..65535). Feeding those in raw is far outside the trained input range.
    """
    a = normalize_for_orb(img).astype(np.float32)
    return torch.from_numpy(a)[None, None].repeat(1, 3, 1, 1)


def _frontend_disk_lightglue(ref, mov, model="euclidean", n_keypoints=2048, **kwargs):
    """DISK + LightGlue -- the learned matcher VALIS and the ACROBAT winners use.

    Returns ``(M0, info)`` with ``residual_px`` and ``n_inliers`` from
    :func:`estimate_transform_from_matches`, exactly like the feature front-ends it
    replaces, so the reporting contract downstream is unchanged.

    MEMORY. DISK is a U-Net: its activation memory scales with image AREA, not with
    keypoint count, and it is ~20x ORB's peak at equal size (measured on the pinned stack:
    3.03 GB at 512 px, 8.78 GB at 1024 px -> GB ~= 1.1 + 7.3*Mpx). The thumbnail size this
    runs on is governed by the STARE `coarse_max_dim` tier (see lib/RegPresets.groovy) --
    a future task is expected to size TILED_COARSE's memory request off that same bound,
    but as of this commit the tier and the process memory request are independent and
    neither has been changed here. Raising the thumbnail bound is a memory decision, not
    a free accuracy knob.
    """
    # BOTH imports live here, inside this function, for two reasons: a torch-present /
    # kornia-absent environment must still get the actionable RuntimeError rather than a
    # bare ImportError, and the confinement guard named above scans for exactly this
    # function. Do not hoist either import to module scope or into a helper.
    try:
        import torch
        from kornia.feature import DISK, LightGlue
    except ImportError as exc:
        raise RuntimeError(
            "frontend='disk_lightglue' needs torch + kornia. They ship in the "
            "bolt3x/mirage-tiled image; install torch==2.3.1 (CPU wheel) + "
            "kornia==0.7.3 to run this outside a container."
        ) from exc

    device = torch.device("cpu")
    disk, matcher = _disk_models(device, DISK, LightGlue)

    t0 = time.perf_counter()
    with torch.inference_mode():
        t_ref = _to_disk_tensor(ref, torch).to(device)
        t_mov = _to_disk_tensor(mov, torch).to(device)
        # pad_if_not_divisible: DISK's U-Net downsamples by 16 and raises on a thumbnail
        # whose dimensions are not multiples of 16. Thumbnail sizes come from
        # decimation_factor(), which produces arbitrary sizes -- so this is not optional.
        feat_kw = dict(
            n=n_keypoints, window_size=5, score_threshold=0.0, pad_if_not_divisible=True
        )
        f_ref = disk(t_ref, **feat_kw)[0]
        f_mov = disk(t_mov, **feat_kw)[0]
        t_feat = time.perf_counter() - t0
        logger.info(
            f"coarse: DISK reference -> {len(f_ref.keypoints)} keypoints | "
            f"moving -> {len(f_mov.keypoints)} keypoints in {t_feat:.1f}s "
            f"({ref.shape[1]}x{ref.shape[0]} / {mov.shape[1]}x{mov.shape[0]})"
        )

        def _side(f, t):
            return {
                "keypoints": f.keypoints[None],
                "descriptors": f.descriptors[None],
                # kornia's own LightGlue fallback for this key is
                # ``data0["image"].shape[-2:][::-1]`` -- i.e. (W, H), NOT (H, W).
                # normalize_keypoints() divides (x, y) keypoints by this size, so a
                # reversed size puts H on the x axis and silently sends every
                # normalised keypoint out of [-1, 1], which _forward()'s internal
                # KORNIA_CHECK then rejects. A SQUARE fixture cannot catch this --
                # (H, W) and (W, H) are the same tuple when H == W -- which is why
                # this shipped broken once already. Do not "tidy" the [::-1] away.
                "image_size": torch.tensor(t.shape[-2:][::-1], device=device)[None],
            }

        out = matcher({"image0": _side(f_ref, t_ref), "image1": _side(f_mov, t_mov)})
        matches = out["matches"][0].cpu().numpy()

    kp_ref = f_ref.keypoints.cpu().numpy()
    kp_mov = f_mov.keypoints.cpu().numpy()
    logger.info(
        f"coarse: {len(matches)} LightGlue matches -> {model} fit "
        f"(DISK+LightGlue total {time.perf_counter() - t0:.1f}s)"
    )
    if len(matches) < _MIN_SAMPLES[model]:
        # estimate_transform_from_matches would fall through to an unconstrained
        # .estimate() and hand back a silently meaningless M0; say so while the slide
        # names are still in the surrounding log context.
        logger.warning(
            f"coarse: only {len(matches)} matches for a {model} fit "
            f"(needs >= {_MIN_SAMPLES[model]}) -- M0 will be unreliable"
        )

    # LightGlue's `matches` column 0 indexes image0 (reference), column 1 indexes image1
    # (moving). DISK keypoints are ALREADY (x, y) -- unlike skimage's ORB/SIFT, which are
    # (row, col) and needed a [:, ::-1] reversal. Reversing these silently transposes M0.
    src = kp_mov[matches[:, 1]]
    dst = kp_ref[matches[:, 0]]
    m, residual, n_inliers = estimate_transform_from_matches(src, dst, model=model, **kwargs)
    return m, {"residual_px": residual, "n_inliers": n_inliers}


_FRONTENDS = {
    "orb": _frontend_orb,
    "sift": _frontend_sift,
    "fourier_mellin": _frontend_fourier_mellin,
    "disk_lightglue": _frontend_disk_lightglue,
}


def estimate_affine(ref, mov, frontend="orb", **kw):
    """Estimate ``M0`` mapping ``mov`` image coordinates onto ``ref`` via a selectable front-end.

    Returns ``(M0, info)`` where ``info`` is a dict that always carries ``frontend`` plus
    whatever the chosen front-end reports (``residual_px``, ``n_inliers``). ``frontend`` selects
    among :data:`FRONTENDS`; ``orb`` is the default and preserves this module's pre-existing
    behaviour.
    """
    if frontend not in _FRONTENDS:
        raise ValueError(f"unknown frontend {frontend!r}; expected one of {FRONTENDS}")
    m0, info = _FRONTENDS[frontend](ref, mov, **kw)
    info["frontend"] = frontend
    return m0, info
