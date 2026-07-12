import json, subprocess, sys
from pathlib import Path
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
    tifffile.imwrite(imgp, img.astype(np.float32))   # (C,Y,X)
    out = tmp_path / "p_seg_eval.json"
    subprocess.run([sys.executable, "bin/seg_quality_eval.py",
                    "--cell-mask", str(cp), "--nuclei-mask", str(npth),
                    "--image", str(imgp),
                    "--id", "p", "--out", str(out),
                    "--pixel-size-um", "0.5"], check=True)
    doc = json.loads(out.read_text())
    assert doc["id"] == "p"
    assert isinstance(doc["QualityScore"], float)
