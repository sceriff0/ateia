import json
import subprocess
import sys

import numpy as np
import tifffile

from tests.cse_fixture import make_arrays


def test_cli_writes_quality_score(tmp_path):
    # Synthetic fixture: img (C,Y,X), cell/nuc label masks (Y,X). Write as the
    # separate label TIFFs + multichannel image that Mirage's SEGMENT emits.
    img, cell, nuc = make_arrays()
    cp = tmp_path / "p_cell_mask.tif"
    npth = tmp_path / "p_nuclei_mask.tif"
    imgp = tmp_path / "p_image.tif"
    tifffile.imwrite(cp, cell.astype(np.int32))
    tifffile.imwrite(npth, nuc.astype(np.int32))
    tifffile.imwrite(imgp, img.astype(np.float32))  # (C,Y,X)
    out = tmp_path / "p_seg_eval.json"
    subprocess.run(
        [
            sys.executable,
            "bin/seg_quality_eval.py",
            "--cell-mask",
            str(cp),
            "--nuclei-mask",
            str(npth),
            "--image",
            str(imgp),
            "--id",
            "p",
            "--out",
            str(out),
            "--pixel-size-um",
            "0.5",
        ],
        check=True,
    )
    doc = json.loads(out.read_text())
    assert doc["id"] == "p"
    assert isinstance(doc["QualityScore"], float)
    assert doc["downsample_factor"] == 1
    assert doc["effective_pixel_size_um"] == 0.5


def test_cli_squeezes_singleton_leading_axis_mask(tmp_path):
    # Some label TIFFs are written as (1,Y,X) rather than (Y,X). Without
    # .squeeze(), _downsample_factor's `y, x = shape` unpack would raise
    # (3 values to unpack). This confirms the CLI tolerates that shape and
    # still writes a normal, non-degenerate result.
    img, cell, nuc = make_arrays()
    cp = tmp_path / "p_cell_mask.tif"
    npth = tmp_path / "p_nuclei_mask.tif"
    imgp = tmp_path / "p_image.tif"
    tifffile.imwrite(cp, cell[np.newaxis, :, :].astype(np.int32))  # (1,Y,X)
    tifffile.imwrite(npth, nuc[np.newaxis, :, :].astype(np.int32))  # (1,Y,X)
    tifffile.imwrite(imgp, img.astype(np.float32))  # (C,Y,X)
    out = tmp_path / "p_seg_eval.json"
    result = subprocess.run(
        [
            sys.executable,
            "bin/seg_quality_eval.py",
            "--cell-mask",
            str(cp),
            "--nuclei-mask",
            str(npth),
            "--image",
            str(imgp),
            "--id",
            "p",
            "--out",
            str(out),
            "--pixel-size-um",
            "0.5",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    doc = json.loads(out.read_text())
    assert doc["id"] == "p"
    assert isinstance(doc["QualityScore"], float)
    assert doc["downsample_factor"] == 1
    # The chosen downsample factor / effective pixel size is logged so it
    # lands in the task's .command.log (stderr, via logging.basicConfig).
    assert "downsample_factor=1" in result.stderr
    assert "effective_pixel_size_um" in result.stderr
