"""Full-reference metrics for VALIDATION against synthetic ground truth.

These need a known clean image, so they run only on the synthetic phantom
(`tests/testdata/generate_synthetic_mosaic.py`), never on the real cluster mosaic.
Their job is to *prove the no-reference ranking is trustworthy*: on data where the
correct answer is known, an over-subtracting variant must score worse than the
ground-truth-faithful one. SSIM is the key anti-gaming term (Wang et al., IEEE TIP
2004): a zeroed image sends the luminance term μ→0 and collapses the score.

Implemented dependency-free (scipy only) so nothing here needs the optional
scikit-image install.
"""
from __future__ import annotations
import numpy as np
from scipy.ndimage import gaussian_filter


def rmse(x, y) -> float:
    x = x.astype(np.float64); y = y.astype(np.float64)
    return float(np.sqrt(np.mean((x - y) ** 2)))


def psnr(x, y, data_range=None) -> float:
    x = x.astype(np.float64); y = y.astype(np.float64)
    mse = np.mean((x - y) ** 2)
    if mse <= 1e-12:
        return float("inf")
    L = float(data_range) if data_range is not None else float(x.max() - x.min()) or 1.0
    return float(10.0 * np.log10(L * L / mse))


def ssim(x, y, data_range=None, sigma=1.5) -> float:
    """Structural similarity index (Wang et al. 2004), global mean over the map.

    Gaussian window (sigma=1.5) per the original paper; C1=(0.01 L)^2,
    C2=(0.03 L)^2 with L the dynamic range. Returns a scalar in [-1, 1]; for a
    zeroed `y`, μ_y→0 and σ_y→0 drive it toward 0.
    """
    x = x.astype(np.float64); y = y.astype(np.float64)
    L = float(data_range) if data_range is not None else (float(x.max() - x.min()) or 1.0)
    C1 = (0.01 * L) ** 2
    C2 = (0.03 * L) ** 2

    mu_x = gaussian_filter(x, sigma)
    mu_y = gaussian_filter(y, sigma)
    mu_x2, mu_y2, mu_xy = mu_x * mu_x, mu_y * mu_y, mu_x * mu_y
    sig_x2 = gaussian_filter(x * x, sigma) - mu_x2
    sig_y2 = gaussian_filter(y * y, sigma) - mu_y2
    sig_xy = gaussian_filter(x * y, sigma) - mu_xy

    ssim_map = ((2 * mu_xy + C1) * (2 * sig_xy + C2)) / \
               ((mu_x2 + mu_y2 + C1) * (sig_x2 + sig_y2 + C2))
    return float(np.clip(ssim_map.mean(), -1.0, 1.0))


def flatfield_error(recovered_ff, true_ff) -> float:
    """RMSE between a recovered flat-field and the injected ground-truth field.

    Both are normalized to mean 1 first (flat-fields are defined up to scale), so
    this measures shape error, not a global gain difference.
    """
    r = recovered_ff.astype(np.float64); t = true_ff.astype(np.float64)
    r = r / (r.mean() + 1e-12)
    t = t / (t.mean() + 1e-12)
    if r.shape != t.shape:                  # compare on the smaller common grid
        from scipy.ndimage import zoom
        r = zoom(r, (t.shape[0] / r.shape[0], t.shape[1] / r.shape[1]), order=1)
    return float(np.sqrt(np.mean((r - t) ** 2)))
