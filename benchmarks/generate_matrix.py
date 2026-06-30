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
    """Resize a 2-D array toward target (H, W), preserving dtype.

    Uses pyvips when its native library loads, else PIL. The returned array's
    shape may differ from target_hw by ~1px due to backend rounding; callers
    should read the actual shape rather than assume target_hw.
    """
    th, tw = target_hw
    try:
        import pyvips
    except (ImportError, OSError):
        # ImportError: package missing. OSError: pyvips installed but libvips.so won't load.
        from PIL import Image
        if src_2d.dtype == np.uint8:
            im = Image.fromarray(src_2d)
        elif src_2d.dtype in (np.uint16, np.int32):
            im = Image.fromarray(src_2d.astype(np.int32), mode="I")
        else:
            raise ValueError(
                f"PIL fallback cannot resize dtype {src_2d.dtype}; install pyvips for this dtype"
            )
        im = im.resize((tw, th), Image.BILINEAR)
        return np.asarray(im, dtype=src_2d.dtype)

    fmt = _VIPS_FORMATS.get(src_2d.dtype.name)
    if fmt is None:
        raise ValueError(f"Unsupported dtype for pyvips resize: {src_2d.dtype}")
    vi = pyvips.Image.new_from_memory(
        src_2d.tobytes(), src_2d.shape[1], src_2d.shape[0], 1, fmt
    )
    vi = vi.resize(tw / src_2d.shape[1], vscale=th / src_2d.shape[0])
    buf = vi.write_to_memory()
    # Use the backend's ACTUAL output dims (pyvips may round +/-1px) to avoid reshape errors.
    return np.frombuffer(buf, dtype=src_2d.dtype).reshape(vi.height, vi.width)


def _read_source_2d(path: Path) -> np.ndarray:
    """Read an image via tifffile and reduce it to a single 2-D channel."""
    import tifffile
    arr = np.squeeze(tifffile.imread(path))
    while arr.ndim > 2:
        # Collapse the smallest leading axis (assumed channel/Z/T) by taking index 0.
        axis = 0 if arr.shape[0] <= arr.shape[-1] else arr.ndim - 1
        arr = arr.take(indices=0, axis=axis)
    if arr.ndim != 2:
        raise ValueError(f"Could not reduce source image to 2-D; got shape {arr.shape}")
    return arr


def run_matrix(source, outdir, target_px, n_channels, seed=0, paired: bool = False,
               n_moving: int = 1):
    import tifffile

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    src = _read_source_2d(Path(source))
    manifest_path = outdir / "matrix_manifest.csv"

    base_fieldnames = ["cell_id", "target_px", "width", "height", "n_channels", "bytes", "path"]
    # `moving_paths` is a ';'-joined list of moving images (one per extra registration
    # panel) so the sweep can register N images, not just a pair. Empty for n_channels==1.
    fieldnames = base_fieldnames + ["moving_paths"] if paired else base_fieldnames

    with open(manifest_path, "w", newline="") as fh:
        # lineterminator='\n' (not csv's default '\r\n') so downstream bash/awk column
        # parsing in run_sweep.sh doesn't see a trailing '\r' on the last field.
        writer = csv.DictWriter(fh, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for tpx in target_px:
            th, tw = compute_target_shape(src.shape, tpx)
            resized = _resize(src, (th, tw))
            rh, rw = resized.shape  # actual dims (may differ from th,tw by backend rounding)
            for nch in n_channels:
                cell_id = f"px{tpx}_ch{nch}"
                out_path = outdir / f"{cell_id}.ome.tif"
                stack = synthesize_channels(resized, nch, seed=seed)
                data = stack[0] if nch == 1 else stack
                metadata = {"axes": "YX" if nch == 1 else "CYX"}
                if nch > 1:
                    metadata["Channel"] = {"Name": ["DAPI"] + [f"ch{i}" for i in range(1, nch)]}
                tifffile.imwrite(out_path, data, photometric="minisblack", metadata=metadata)
                row = {
                    "cell_id": cell_id, "target_px": tpx, "width": rw, "height": rh,
                    "n_channels": nch, "bytes": out_path.stat().st_size, "path": str(out_path),
                }
                if paired:
                    moving_paths = []
                    if nch >= 2:
                        # One distinct moving image per extra registration panel. Each gets a
                        # different seed (distinct content) and a distinct channel-name set
                        # (DAPI|m{panel}_1|...) so the pipeline's duplicate-channel guard accepts them.
                        for j in range(1, n_moving + 1):
                            mov_stack = synthesize_channels(resized, nch, seed=seed + j)
                            mov_out = outdir / f"{cell_id}_moving{j}.ome.tif"
                            tifffile.imwrite(
                                mov_out, mov_stack, photometric="minisblack",
                                metadata={"axes": "CYX",
                                          "Channel": {"Name": ["DAPI"] + [f"m{j}_{i}" for i in range(1, nch)]}})
                            moving_paths.append(str(mov_out))
                    row["moving_paths"] = ";".join(moving_paths)
                writer.writerow(row)
    return manifest_path


def derive_from_sweep(sweep_path) -> dict:
    """Read a sweep.yaml and derive the matrix it needs: target_px, n_channels, n_moving.

    Each value covers the axis values UNION the baseline, so every run the sweep will
    launch finds its cell. n_moving = max(n_register_images) - 1 so enough distinct moving
    panels exist. Returns {target_px, n_channels, n_moving, paired}.
    """
    import yaml

    sweep = yaml.safe_load(Path(sweep_path).read_text())
    baseline = sweep.get("baseline", {})
    axes = sweep.get("axes", {})

    def values_for(key, default):
        vals = set(axes.get(key, []))
        if key in baseline:
            vals.add(baseline[key])
        return sorted(vals) if vals else default

    target_px = values_for("target_px", [2048, 4096, 8192, 16384, 32768])
    n_channels = values_for("n_channels", [1, 2, 4, 8])
    n_reg = values_for("n_register_images", [2])
    n_moving = max(max(n_reg) - 1, 1)
    # A sweep with >1 panels (or any multi-channel registration) needs paired moving images.
    paired = max(n_reg) > 1 or max(n_channels) >= 2
    return {"target_px": target_px, "n_channels": n_channels, "n_moving": n_moving, "paired": paired}


def main():
    ap = argparse.ArgumentParser(description="Generate a (size x channels) benchmark matrix.")
    ap.add_argument("--source", required=True, type=Path, help="Source image (user-supplied).")
    ap.add_argument("--outdir", required=True, type=Path)
    ap.add_argument("--sweep", type=Path, default=None,
                    help="Derive --target-px, --n-channels, --n-moving, and --paired straight "
                         "from a sweep.yaml so the matrix matches the sweep with no manual sync. "
                         "Explicit flags below override the derived values.")
    ap.add_argument("--target-px", type=int, nargs="+", default=None)
    ap.add_argument("--n-channels", type=int, nargs="+", default=None)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--paired", action="store_true", default=None,
                    help="Also emit moving image(s) per cell (n_channels>=2) with distinct channel names.")
    ap.add_argument("--n-moving", type=int, default=None,
                    help="Moving images per paired cell (=extra registration panels). "
                         "Set >= max(n_register_images)-1 to benchmark N-image registration.")
    a = ap.parse_args()

    # Start from sweep-derived values (if given), then let explicit flags override.
    d = derive_from_sweep(a.sweep) if a.sweep else {}
    target_px = a.target_px if a.target_px is not None else d.get("target_px", [2048, 4096, 8192, 16384, 32768, 65536, 131072])
    n_channels = a.n_channels if a.n_channels is not None else d.get("n_channels", [1, 2, 4, 8])
    n_moving = a.n_moving if a.n_moving is not None else d.get("n_moving", 1)
    paired = a.paired if a.paired is not None else d.get("paired", False)

    path = run_matrix(a.source, a.outdir, target_px, n_channels, a.seed,
                      paired=paired, n_moving=n_moving)
    print(f"Wrote matrix manifest: {path}")


if __name__ == "__main__":
    main()
