"""Per-step diagnostic plots. Uses the Agg backend (headless-safe)."""
from __future__ import annotations
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from illum.metrics import _detrend


def _thumb(img, target=800):
    ds = max(max(img.shape) // target, 1)
    return img[::ds, ::ds]


def plot_grid_recovery(stack, grid, out_path, approx_tile=None):
    img = (stack.sum(axis=0) if stack.ndim == 3 else stack).astype(np.float64)
    col = np.median(img, axis=0)
    row = np.median(img, axis=1)
    fig, ax = plt.subplots(2, 2, figsize=(11, 8))
    ax[0, 0].plot(col); ax[0, 0].set_title("Column profile (X)")
    ax[0, 1].plot(row); ax[0, 1].set_title("Row profile (Y)")
    x = _detrend(col); x = (x - x.mean()) * np.hanning(len(x))
    ac = np.correlate(x, x, mode="full")[len(x) - 1:]; ac /= ac[0] + 1e-12
    ax[1, 0].plot(ac[:min(len(ac), int(3 * grid["pitch_x"]))])
    ax[1, 0].axvline(grid["pitch_x"], color="r", ls="--",
                     label=f"pitch_x={grid['pitch_x']:.1f}")
    ax[1, 0].legend(); ax[1, 0].set_title("Autocorrelation (X)")
    th = _thumb(img)
    ax[1, 1].imshow(th, cmap="gray")
    step = grid["pitch_x"] / (img.shape[1] / th.shape[1])
    for k in range(int(th.shape[1] / step) + 1):
        ax[1, 1].axvline(grid["phase_x"] / (img.shape[1] / th.shape[1]) + k * step,
                         color="c", lw=0.4)
    ax[1, 1].set_title("Recovered grid overlay")
    fig.tight_layout(); fig.savefig(out_path, dpi=90); plt.close(fig)
    return str(out_path)


def plot_flatfield(ff, out_path, channel_name):
    fig, ax = plt.subplots(figsize=(5, 4))
    im = ax.imshow(ff, cmap="viridis"); fig.colorbar(im, ax=ax)
    ax.set_title(f"Flat-field — {channel_name} (p-p {np.ptp(ff) * 100:.1f}%)")
    fig.tight_layout(); fig.savefig(out_path, dpi=90); plt.close(fig)
    return str(out_path)


def plot_before_after(before, after, grid, out_path, channel_name):
    fig, ax = plt.subplots(2, 2, figsize=(11, 8))
    ax[0, 0].imshow(_thumb(before), cmap="gray"); ax[0, 0].set_title("Before")
    ax[0, 1].imshow(_thumb(after), cmap="gray"); ax[0, 1].set_title("After")
    ax[1, 0].plot(np.median(before, axis=0), label="before")
    ax[1, 0].plot(np.median(after, axis=0), label="after")
    ax[1, 0].legend(); ax[1, 0].set_title("Column profile")
    for lbl, arr in (("before", before), ("after", after)):
        p = _detrend(np.median(arr, axis=0).astype(np.float64))
        p = (p - p.mean()) * np.hanning(len(p))
        power = np.abs(np.fft.rfft(p)) ** 2
        ax[1, 1].semilogy(np.fft.rfftfreq(len(p)), power + 1e-9, label=lbl)
    ax[1, 1].axvline(1.0 / grid["pitch_x"], color="r", ls="--", label="tile freq")
    ax[1, 1].legend(); ax[1, 1].set_title("Power spectrum (X)")
    fig.suptitle(channel_name)
    fig.tight_layout(); fig.savefig(out_path, dpi=90); plt.close(fig)
    return str(out_path)
