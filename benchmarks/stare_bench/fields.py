"""Ground-truth deformation fields, drawn from model classes no competitor fits.

THE CIRCULARITY FIREWALL LIVES HERE. If the ground-truth warp came from the
same model class STARE fits -- a smooth bilinear control grid -- the benchmark
would be rigged in STARE's favour and a reviewer would say so. The headline
family is therefore a band-limited random Fourier field: a complex spectrum
with a power-law rolloff, inverse-FFT'd to a smooth displacement. That field is
in the hypothesis space of NO competitor -- not STARE's control grid, not
VALIS's B-spline/TPS, not ASHLAR's rigid per-tile shift.

``sample(xy)`` is the PRIMARY interface, not ``dense()``. At the gigapixel rung
a dense field is ~20 GB, so ground truth is recovered on demand from
``{seed, family, params}`` -- exact, because generation is pure.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "Field",
    "make_field",
    "FAMILIES",
    "DENSE_MAX_PIXELS",
    "REG_TILED_TILE",
    "MAX_COARSE_PITCH_PX",
]

FAMILIES = ("random_fourier", "affine", "adversarial")

# 64 Mpx * 2 components * 8 bytes (dtype=float, i.e. float64) = ~1 GB, the
# largest dense field we will hold.
DENSE_MAX_PIXELS = 64_000_000

# STARE's reg_tiled_tile at its default reg_tiled_mode=high.
REG_TILED_TILE = 2048

# The random_fourier coarse-grid pitch must stay well below STARE's own
# control-grid spacing, or STARE's grid could represent the "ground truth"
# almost exactly -- the circularity this module exists to forbid.
MAX_COARSE_PITCH_PX = REG_TILED_TILE / 4


class Field:
    """A ground-truth displacement field over an ``(H, W)`` raster.

    ``sample(xy)`` returns ``(dx, dy)`` at arbitrary float coordinates; it is
    the interface every metric uses. ``dense()`` is a convenience for small
    rasters only.
    """

    def __init__(self, shape, params, sampler):
        self.shape = (int(shape[0]), int(shape[1]))
        self.params = dict(params)
        self._sampler = sampler

    def sample(self, xy):
        xy = np.asarray(xy, dtype=float)
        if xy.ndim != 2 or xy.shape[1] != 2:
            raise ValueError(f"expected (N, 2) points, got {xy.shape}")
        return np.asarray(self._sampler(xy), dtype=float)

    def dense(self):
        h, w = self.shape
        if h * w > DENSE_MAX_PIXELS:
            raise MemoryError(
                f"dense field for {h}x{w} exceeds DENSE_MAX_PIXELS "
                f"({DENSE_MAX_PIXELS}); use sample(xy) per tile instead"
            )
        ys, xs = np.mgrid[0:h, 0:w]
        xy = np.stack([xs.ravel(), ys.ravel()], axis=1).astype(float)
        return self.sample(xy).reshape(h, w, 2)


def _random_fourier(shape, seed, correlation_px=512.0, amplitude_px=12.0,
                    rolloff=3.0):
    """Band-limited random field: power-law spectrum, inverse FFT, interpolated.

    ``correlation_px`` sets the characteristic scale (coarse-grid pitch is
    approximately ``correlation_px / 8``). The number that actually matters
    for the circularity firewall is the DERIVED coarse-grid pitch, not
    ``correlation_px`` itself: this field is structurally a bilinear
    interpolant over a control grid, the same model class STARE fits, so the
    pitch is bounded to ``MAX_COARSE_PITCH_PX`` (a quarter of STARE's
    ``reg_tiled_tile``) to keep it far finer than anything STARE's grid could
    represent.
    """
    h, w = int(shape[0]), int(shape[1])
    rng = np.random.default_rng(seed)
    # Build on a coarse raster and interpolate: the field is band-limited, so
    # resolving it at full slide resolution buys nothing and costs everything.
    gh = max(8, min(256, int(np.ceil(h / max(1.0, correlation_px / 8)))))
    gw = max(8, min(256, int(np.ceil(w / max(1.0, correlation_px / 8)))))

    pitch_h = (h - 1) / (gh - 1) if gh > 1 else float(h)
    pitch_w = (w - 1) / (gw - 1) if gw > 1 else float(w)
    if pitch_h > MAX_COARSE_PITCH_PX or pitch_w > MAX_COARSE_PITCH_PX:
        raise ValueError(
            f"random_fourier derived coarse-grid pitch ({pitch_h:.1f} x "
            f"{pitch_w:.1f} px) for shape={shape!r} at "
            f"correlation_px={correlation_px} exceeds MAX_COARSE_PITCH_PX "
            f"({MAX_COARSE_PITCH_PX} px): a pitch at or above STARE's own "
            f"control-grid spacing would make the ground-truth field "
            f"representable by the model under test."
        )

    fy = np.fft.fftfreq(gh)[:, None]
    fx = np.fft.fftfreq(gw)[None, :]
    radius = np.sqrt(fy**2 + fx**2)
    radius[0, 0] = 1.0
    spectrum = radius ** (-rolloff / 2.0)
    spectrum[0, 0] = 0.0  # no DC: a constant offset is a translation, not a warp

    comps = []
    for _ in range(2):
        noise = rng.normal(size=(gh, gw)) + 1j * rng.normal(size=(gh, gw))
        grid = np.real(np.fft.ifft2(noise * spectrum))
        peak = np.abs(grid).max()
        comps.append(grid / peak * amplitude_px if peak > 0 else grid)

    gxs = np.linspace(0.0, w - 1.0, gw)
    gys = np.linspace(0.0, h - 1.0, gh)

    def sampler(xy):
        out = np.empty((xy.shape[0], 2), dtype=float)
        for k, grid in enumerate(comps):
            out[:, k] = _bilinear_on_grid(grid, gxs, gys, xy)
        return out

    params = {
        "family": "random_fourier",
        "seed": int(seed),
        "correlation_px": float(correlation_px),
        "amplitude_px": float(amplitude_px),
        "rolloff": float(rolloff),
        "coarse_shape": [gh, gw],
    }
    return params, sampler


def _bilinear_on_grid(grid, gxs, gys, xy):
    """Bilinear lookup of ``grid`` at ``xy``, edge-clamped."""
    x = np.clip(xy[:, 0], gxs[0], gxs[-1])
    y = np.clip(xy[:, 1], gys[0], gys[-1])
    ix1 = np.clip(np.searchsorted(gxs, x, side="right"), 1, gxs.size - 1)
    iy1 = np.clip(np.searchsorted(gys, y, side="right"), 1, gys.size - 1)
    ix0, iy0 = ix1 - 1, iy1 - 1
    tx = (x - gxs[ix0]) / (gxs[ix1] - gxs[ix0])
    ty = (y - gys[iy0]) / (gys[iy1] - gys[iy0])
    top = grid[iy0, ix0] * (1 - tx) + grid[iy0, ix1] * tx
    bot = grid[iy1, ix0] * (1 - tx) + grid[iy1, ix1] * tx
    return top * (1 - ty) + bot * ty


def _affine(shape, seed, rotation_deg=None, scale=None, translation=None):
    """Global affine displacement -- the sanity rung.

    Any metric that cannot score an exactly-linear field is broken, which is
    what makes this family worth keeping alongside the hard ones.
    """
    rng = np.random.default_rng(seed)
    rot = float(rng.uniform(-2.0, 2.0)) if rotation_deg is None else float(rotation_deg)
    sc = float(rng.uniform(0.995, 1.005)) if scale is None else float(scale)
    if translation is None:
        translation = tuple(rng.uniform(-20.0, 20.0, size=2))
    tx, ty = float(translation[0]), float(translation[1])

    theta = np.deg2rad(rot)
    cx, cy = shape[1] / 2.0, shape[0] / 2.0
    m = sc * np.array([[np.cos(theta), -np.sin(theta)],
                       [np.sin(theta), np.cos(theta)]])

    def sampler(xy):
        centred = xy - np.array([cx, cy])
        moved = centred @ m.T + np.array([cx + tx, cy + ty])
        return moved - xy

    params = {
        "family": "affine",
        "seed": int(seed),
        "rotation_deg": rot,
        "scale": sc,
        "translation": [tx, ty],
    }
    return params, sampler


def _adversarial(shape, seed, n_folds=3, fold_amplitude_px=40.0,
                 fold_sigma_px=180.0):
    """Localised high-gradient bumps: folds and tears.

    These are the cases that break the fixed-point inverse in
    ``bin/utils/tiled_warp.py:_invert``, whose three iterations converge only
    when the field's Lipschitz constant is below 1. Nothing in the pipeline
    checks that, so the benchmark must supply fields that violate it.
    """
    rng = np.random.default_rng(seed)
    h, w = int(shape[0]), int(shape[1])
    centres = np.stack([rng.uniform(0, w, n_folds), rng.uniform(0, h, n_folds)], axis=1)
    directions = rng.normal(size=(n_folds, 2))
    directions /= np.linalg.norm(directions, axis=1, keepdims=True)

    def sampler(xy):
        out = np.zeros((xy.shape[0], 2), dtype=float)
        for c, d in zip(centres, directions):
            r2 = ((xy - c) ** 2).sum(axis=1)
            weight = np.exp(-r2 / (2.0 * fold_sigma_px**2))
            out += fold_amplitude_px * weight[:, None] * d[None, :]
        return out

    params = {
        "family": "adversarial",
        "seed": int(seed),
        "n_folds": int(n_folds),
        "fold_amplitude_px": float(fold_amplitude_px),
        "fold_sigma_px": float(fold_sigma_px),
        "centres": centres.tolist(),
        "directions": directions.tolist(),
    }
    return params, sampler


_BUILDERS = {
    "random_fourier": _random_fourier,
    "affine": _affine,
    "adversarial": _adversarial,
}


def make_field(family, shape, seed, **params):
    """Build a ground-truth field. Pure: same (family, shape, seed, params) -> same field."""
    if family not in _BUILDERS:
        raise ValueError(f"unknown field family {family!r}; expected one of {FAMILIES}")
    built, sampler = _BUILDERS[family](shape, seed, **params)
    return Field(shape, built, sampler)
