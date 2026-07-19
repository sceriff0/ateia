import subprocess, sys, json, pathlib
import tifffile
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_benchmark_produces_report_and_metrics(tmp_path):
    d = make_synthetic_mosaic(tile=96, overlap=12, n_channels=2, dark=150.0,
                              vignette_strength=0.5)
    src = tmp_path / "mosaic.tif"
    tifffile.imwrite(src, d["mosaic"], metadata={"axes": "CYX"})
    outdir = tmp_path / "bench"
    r = subprocess.run([sys.executable, str(ROOT / "bin" / "illum_benchmark.py"),
                        "--image", str(src), "--outdir", str(outdir),
                        "--channels", "CH0", "CH1", "--approx-tile", str(d["pitch"]),
                        "--no-pyramids"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (outdir / "report.html").exists()
    metrics = json.loads((outdir / "metrics.json").read_text())
    assert len(metrics) >= 3
    assert all("seam_x_after" in m["metrics"] for m in metrics)
