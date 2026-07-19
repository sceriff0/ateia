import subprocess, sys, pathlib
import numpy as np, tifffile
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_cli_writes_corrected_ome_tiff(tmp_path):
    d = make_synthetic_mosaic(tile=96, overlap=12, n_channels=2, dark=150.0)
    src = tmp_path / "mosaic.tif"
    tifffile.imwrite(src, d["mosaic"], metadata={"axes": "CYX"})
    out = tmp_path / "out"
    out.mkdir()
    r = subprocess.run([sys.executable, str(ROOT / "bin" / "illum_correct.py"),
                        "--image", str(src), "--output_dir", str(out),
                        "--channels", "CH0", "CH1", "--approx-tile", str(d["pitch"]),
                        "--dark", "const"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    produced = list(out.glob("*_periodic.ome.tif"))
    assert produced, r.stdout + r.stderr
    arr = tifffile.imread(produced[0])
    assert arr.shape == d["mosaic"].shape
