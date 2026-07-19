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


def _run(args, **kw):
    return subprocess.run([sys.executable, str(ROOT / "bin" / "illum_benchmark.py"), *args],
                          capture_output=True, text=True, **kw)


def test_parallel_variant_then_aggregate_matches_contract(tmp_path):
    # The SLURM-array workflow: --list-variants -> per-variant --variant NAME
    # -> --aggregate must reconstruct metrics.json + report.html from parts/.
    d = make_synthetic_mosaic(tile=96, overlap=12, n_channels=2, dark=150.0,
                              vignette_strength=0.5)
    src = tmp_path / "mosaic.tif"
    tifffile.imwrite(src, d["mosaic"], metadata={"axes": "CYX"})
    outdir = tmp_path / "bench"
    outdir.mkdir()

    # 1) enumerate variants (no image needed)
    lst = _run(["--list-variants", "--outdir", str(outdir)])
    assert lst.returncode == 0, lst.stderr
    names = [n for n in lst.stdout.splitlines() if n.strip()]
    assert len(names) >= 3

    # 2) run each variant as its own task (as a SLURM array element would)
    for name in names:
        r = _run(["--variant", name, "--image", str(src), "--outdir", str(outdir),
                  "--channels", "CH0", "CH1", "--approx-tile", str(d["pitch"]),
                  "--no-pyramids"])
        assert r.returncode == 0, r.stderr
        assert (outdir / "parts" / f"{name}.json").exists()

    # 3) aggregate (no image needed) -> combined report + metrics
    agg = _run(["--aggregate", "--outdir", str(outdir)])
    assert agg.returncode == 0, agg.stderr
    assert (outdir / "report.html").exists()
    metrics = json.loads((outdir / "metrics.json").read_text())
    assert len(metrics) == len(names)
    assert all("seam_x_after" in m["metrics"] for m in metrics)
