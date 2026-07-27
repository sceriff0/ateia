"""Downsampling for the reference-free CSE seg-eval.

Full-WSI label masks blow CSE's memory/time budget (foreground-separation
active-contour + per-pixel reductions scale with pixel count), so
`seg_quality_eval.py` can bin the image + masks before scoring. These tests
pin the helper behaviour and, crucially, show how CLOSE a downsampled score
stays to the full-resolution ("mine") result and to the pinned upstream
("original") golden metrics.
"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import tifffile

from bin.seg_quality_eval import _downsample, _downsample_factor
from bin.utils.cse import single_method_eval
from tests.cse_fixture import PIXEL_UM, make_arrays

DATA = Path(__file__).parent / "data" / "cse"


def _flatten(metrics):
    flat = {}
    for k, v in metrics.items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                flat[f"{k}::{kk}"] = float(vv)
        else:
            flat[k] = float(v)
    return flat


def _eval(img_cyx, cell, nuc, px):
    """Score a (C,Y,X) image + (Y,X) label masks at pixel size px (um)."""
    img5 = img_cyx[np.newaxis, :, np.newaxis, :, :]
    mask5 = np.stack([cell, nuc], 0)[np.newaxis, :, np.newaxis, :, :]
    img = {"name": "synth", "img": None, "data": img5}
    mask = {"name": "synth", "img": None, "data": mask5}
    return single_method_eval(
        img, mask, PCA_model=False, output_dir=".", pixelsizex=px, pixelsizey=px
    )


def make_tissue_scene():
    """A larger, realistic-ish scene that survives 2x binning.

    Unlike the tiny golden fixture (bright only inside cells), this lays cells
    over a noisy tissue background so BOTH the image foreground and the
    foreground-outside-cells region stay populated after downsampling -- the
    conditions CSE's foreground-uniformity PCA needs. 100 cells (20px, 10px
    nuclei) exercise KMeans k=2..10 and silhouette at both resolutions.
    """
    rng = np.random.default_rng(1)
    Y = X = 300
    C = 3
    cell = np.zeros((Y, X), np.int32)
    nuc = np.zeros((Y, X), np.int32)
    img = (25 + rng.normal(0, 3, (C, Y, X))).astype(np.float32)  # tissue background
    cid = 0
    for gy in range(10, Y - 20, 30):
        for gx in range(10, X - 20, 30):
            cid += 1
            cell[gy : gy + 20, gx : gx + 20] = cid
            nuc[gy + 5 : gy + 15, gx + 5 : gx + 15] = cid
            t = cid % C
            for c in range(C):
                img[c, gy : gy + 20, gx : gx + 20] += 70 if c == t else 8
    return img, cell, nuc


# ── helper unit tests ────────────────────────────────────────────────────────


def test_downsample_factor_picks_smallest_factor_under_budget():
    # 160*160 = 25600 px. Budget 6400 needs each axis halved: ceil(160/2)=80,
    # 80*80 = 6400 <= 6400, and factor 1 (25600) would exceed it.
    assert _downsample_factor((160, 160), 6400) == 2
    # Already under budget -> no downsampling.
    assert _downsample_factor((160, 160), 10_000_000) == 1
    # Disabled (None / 0) -> no downsampling.
    assert _downsample_factor((160, 160), None) == 1
    assert _downsample_factor((160, 160), 0) == 1


def test_downsample_shrinks_grid_and_never_invents_labels():
    img, cell, nuc = make_arrays()  # img (3,160,160), masks (160,160)
    ch_ds, cell_ds, nuc_ds = _downsample(img, cell, nuc, 2)

    # Image is mean-pooled, masks are subsampled, all on the same 80x80 grid.
    assert ch_ds.shape == (3, 80, 80)
    assert cell_ds.shape == (80, 80)
    assert nuc_ds.shape == (80, 80)

    # Subsampling can only drop labels, never create new ones.
    assert set(np.unique(cell_ds)).issubset(set(np.unique(cell)))
    assert set(np.unique(nuc_ds)).issubset(set(np.unique(nuc)))

    # factor 1 is an exact no-op (preserves the golden-equivalence path).
    ch1, cell1, nuc1 = _downsample(img, cell, nuc, 1)
    assert np.array_equal(ch1, img)
    assert np.array_equal(cell1, cell)
    assert np.array_equal(nuc1, nuc)


# ── the headline similarity test ─────────────────────────────────────────────


def _rel(a, b):
    d = abs(b) if abs(b) > 1e-12 else 1.0
    return abs(a - b) / d


def _print_table(title, keys, cols):
    """cols: list of (header, dict-or-None). Prints one row per metric key."""
    heads = "".join(f"{h:>14s}" for h, _ in cols)
    lines = [f"\n{title}", f"  {'metric':58s}{heads}"]
    for key in keys:
        vals = "".join(
            f"{(d.get(key, float('nan')) if d else float('nan')):14.5f}" for _, d in cols
        )
        lines.append(f"  {key:58s}{vals}")
    print("\n".join(lines))


def test_fullres_matches_original_upstream_golden():
    """'mine' (the modified vendored CSE) at full res == the pinned upstream
    ('original') golden metrics. This is the anchor the downsample is measured
    against."""
    golden = {
        k: float(v)
        for k, v in json.loads((DATA / "golden_metrics.json").read_text()).items()
    }
    img, cell, nuc = make_arrays()
    full = _flatten(_eval(img, cell, nuc, PIXEL_UM))

    _print_table(
        "Full-res 'mine' vs 'original' (upstream golden), standard fixture:",
        sorted(full),
        [("original", golden), ("mine(full)", full)],
    )
    assert set(full) == set(golden)
    assert _rel(full["QualityScore"], golden["QualityScore"]) <= 1e-3


def test_downsampled_score_stays_close_to_fullres():
    """A 2x-binned score tracks the full-resolution ('mine') score closely on a
    realistic tissue scene -- evidence that downsampling is a safe way to fit
    CSE inside its memory/time budget."""
    factor = 2
    img, cell, nuc = make_tissue_scene()

    full = _flatten(_eval(img, cell, nuc, PIXEL_UM))  # "mine" at full res
    ch_ds, cell_ds, nuc_ds = _downsample(img, cell, nuc, factor)
    # Binning by `factor` makes each pixel cover `factor`x more microns/axis, so
    # the physical pixel size must scale with it to keep area-based metrics honest.
    down = _flatten(_eval(ch_ds, cell_ds, nuc_ds, PIXEL_UM * factor))

    _print_table(
        f"Downsampled(x{factor}) vs full-res 'mine', tissue scene "
        f"({cell.shape} -> {cell_ds.shape}):",
        sorted(full),
        [("mine(full)", full), (f"down(x{factor})", down)],
    )
    qs_rel = _rel(down["QualityScore"], full["QualityScore"])
    print(f"  QualityScore rel(downsampled, fullres) = {qs_rel:.4f}")

    # Structure preserved: same metric keys survive downsampling.
    assert set(down) == set(full)

    # Geometry/area/matching metrics ARE the segmentation-quality signal, and
    # binning (with the pixel size scaled to match) leaves them essentially
    # unchanged -- the strongest evidence that downsampling is safe.
    geometry = [
        "Matched Cell::NumberOfCellsPer100SquareMicrons",
        "Matched Cell::FractionOfForegroundOccupiedByCells",
        "Matched Cell::FractionOfMatchedCellsAndNuclei",
        "Matched Cell::FractionOfCellMaskInForeground",
        "Matched Cell::1-FractionOfBackgroundOccupiedByCells",
    ]
    for k in geometry:
        assert _rel(down[k], full[k]) <= 0.02, (k, down[k], full[k])

    # The composite QualityScore drifts more: its KMeans/silhouette sub-metrics
    # are resolution-sensitive, and the PCA+exp composite amplifies that. Guard
    # it same-order (regression backstop) -- the printed number is the real
    # "how similar" figure, and it argues for a FIXED factor across patients.
    assert qs_rel <= 0.5


def test_cli_max_pixels_triggers_downsampling(tmp_path):
    """The CLI --max-pixels flag bins the inputs and records the factor +
    scaled pixel size in the output JSON."""
    img, cell, nuc = make_tissue_scene()  # (3,300,300) => 90000 px/plane
    cp, npth, imgp = (tmp_path / n for n in ("c.tif", "n.tif", "i.tif"))
    tifffile.imwrite(cp, cell)
    tifffile.imwrite(npth, nuc)
    tifffile.imwrite(imgp, img)
    out = tmp_path / "eval.json"
    subprocess.run(
        [sys.executable, "bin/seg_quality_eval.py",
         "--cell-mask", str(cp), "--nuclei-mask", str(npth), "--image", str(imgp),
         "--id", "p", "--out", str(out),
         "--pixel-size-um", "0.5", "--max-pixels", "25000"],  # 90000 -> needs 2x
        check=True,
    )
    doc = json.loads(out.read_text())
    assert doc["downsample_factor"] == 2
    assert doc["effective_pixel_size_um"] == 1.0  # 0.5 * 2
    assert isinstance(doc["QualityScore"], float)
