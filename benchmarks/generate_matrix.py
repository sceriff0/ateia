"""Generate a (size x channels) benchmark matrix from a single source image.

Pure functions (compute_target_shape, synthesize_channels) are unit-tested.
Heavy I/O (read/resize/write OME-TIFF) is isolated in run_matrix().
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np


def compute_target_shape(src_hw: tuple[int, int], target_long_edge: int) -> tuple[int, int]:
    """Scale (height, width) so the longer edge equals target_long_edge, preserving aspect."""
    h, w = src_hw
    long_edge = max(h, w)
    if long_edge == 0:
        raise ValueError("src_hw must have positive dimensions")
    scale = target_long_edge / long_edge
    return (round(h * scale), round(w * scale))


def synthesize_channels(src_2d: np.ndarray, n_channels: int, seed: int = 0) -> np.ndarray:
    """Replicate a single 2-D channel into n_channels with per-channel perturbation.

    Channel 0 is the unmodified source. Channels 1..N-1 add intensity jitter,
    Gaussian noise, and a c-px roll offset (by channel index) so each channel is non-identical.
    """
    if src_2d.ndim != 2:
        raise ValueError("src_2d must be 2-D (H, W)")
    if n_channels < 1:
        raise ValueError("n_channels must be >= 1")
    if not np.issubdtype(src_2d.dtype, np.integer):
        raise ValueError(f"src_2d must be an integer dtype, got {src_2d.dtype}")
    rng = np.random.default_rng(seed)
    info = np.iinfo(src_2d.dtype)
    out = np.empty((n_channels,) + src_2d.shape, dtype=src_2d.dtype)
    out[0] = src_2d
    for c in range(1, n_channels):
        gain = 1.0 + rng.uniform(-0.1, 0.1)
        noise = rng.normal(0.0, 3.0, size=src_2d.shape)
        shifted = np.roll(src_2d, shift=c, axis=1)
        vals = np.clip(shifted.astype(np.float64) * gain + noise, info.min, info.max)
        out[c] = vals.astype(src_2d.dtype)
    return out


# numpy dtype -> pyvips band format string
_VIPS_FORMATS = {
    "uint8": "uchar", "int8": "char", "uint16": "ushort", "int16": "short",
    "uint32": "uint", "int32": "int", "float32": "float", "float64": "double",
}


def _resize(src_2d: np.ndarray, target_hw: tuple[int, int]) -> np.ndarray:
    """Resize a 2-D array to target (H, W), preserving dtype. Uses pyvips if available, else PIL."""
    th, tw = target_hw
    try:
        import pyvips
    except ModuleNotFoundError:
        from PIL import Image
        orig_dtype = src_2d.dtype
        im = Image.fromarray(src_2d)
        # PIL only supports BILINEAR on modes without a bit-depth suffix (e.g. "I;16").
        # Convert to "I" (int32) for non-uint8 integer types so bilinear resampling works,
        # then cast the result back to the original dtype.
        if im.mode not in ("L", "RGB", "RGBA", "F"):
            im = im.convert("I")
        im = im.resize((tw, th), Image.BILINEAR)
        return np.asarray(im, dtype=orig_dtype)

    fmt = _VIPS_FORMATS.get(src_2d.dtype.name)
    if fmt is None:
        raise ValueError(f"Unsupported dtype for pyvips resize: {src_2d.dtype}")
    vi = pyvips.Image.new_from_memory(
        src_2d.tobytes(), src_2d.shape[1], src_2d.shape[0], 1, fmt
    )
    vi = vi.resize(tw / src_2d.shape[1], vscale=th / src_2d.shape[0])
    buf = vi.write_to_memory()
    return np.frombuffer(buf, dtype=src_2d.dtype).reshape(th, tw)


def _read_source_2d(path: Path) -> np.ndarray:
    import tifffile
    arr = tifffile.imread(path)
    if arr.ndim == 3:  # collapse to a single representative channel
        arr = arr[0] if arr.shape[0] <= arr.shape[-1] else arr[..., 0]
    return arr


def run_matrix(source, outdir, target_px, n_channels, seed=0):
    import tifffile

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    src = _read_source_2d(Path(source))
    manifest_path = outdir / "matrix_manifest.csv"

    with open(manifest_path, "w", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["cell_id", "target_px", "width", "height", "n_channels", "bytes", "path"],
        )
        writer.writeheader()
        for tpx in target_px:
            th, tw = compute_target_shape(src.shape, tpx)
            resized = _resize(src, (th, tw))
            for nch in n_channels:
                cell_id = f"px{tpx}_ch{nch}"
                out_path = outdir / f"{cell_id}.ome.tif"
                stack = synthesize_channels(resized, nch, seed=seed)
                data = stack[0] if nch == 1 else stack
                channel_names = [f"ch{i}" for i in range(nch)]
                tifffile.imwrite(
                    out_path,
                    data,
                    photometric="minisblack",
                    metadata={"axes": "YX" if nch == 1 else "CYX",
                              "Channel": {"Name": channel_names}},
                )
                writer.writerow({
                    "cell_id": cell_id, "target_px": tpx, "width": tw, "height": th,
                    "n_channels": nch, "bytes": out_path.stat().st_size, "path": str(out_path),
                })
    return manifest_path


def main():
    ap = argparse.ArgumentParser(description="Generate a (size x channels) benchmark matrix.")
    ap.add_argument("--source", required=True, type=Path, help="Source image (user-supplied).")
    ap.add_argument("--outdir", required=True, type=Path)
    ap.add_argument("--target-px", type=int, nargs="+", default=[2048, 4096, 8192, 16384, 32768])
    ap.add_argument("--n-channels", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    path = run_matrix(a.source, a.outdir, a.target_px, a.n_channels, a.seed)
    print(f"Wrote matrix manifest: {path}")


if __name__ == "__main__":
    main()
