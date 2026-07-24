import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "generate_resource_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("grr", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_bytes():
    grr = _load()
    assert grr.parse_bytes("3.2 GB") == round(3.2 * 1024**3, 1)
    assert grr.parse_bytes("512 MB") == 512 * 1024**2
    assert grr.parse_bytes("-") is None
    assert grr.parse_bytes("") is None


def test_parse_duration():
    grr = _load()
    assert grr.parse_duration("1.5s") == 1.5
    assert grr.parse_duration("12m 4s") == 724.0
    assert grr.parse_duration("2h 1m") == 7260.0
    assert grr.parse_duration("-") is None


def test_parse_percent():
    grr = _load()
    assert grr.parse_percent("142.3%") == 142.3
    assert grr.parse_percent("-") is None


def test_parse_trace(tmp_path):
    grr = _load()
    t = tmp_path / "trace.txt"
    t.write_text(
        "task_id\tprocess\ttag\tname\tstatus\texit\tsubmit\tstart\tcomplete\t"
        "duration\trealtime\t%cpu\tcpus\tmemory\tpeak_rss\tpeak_vmem\trchar\twchar\n"
        "1\tMIRAGE:PRE:CONVERT_IMAGE\tP001\tname\tCOMPLETED\t0\t-\t-\t-\t"
        "12m 4s\t10m\t142.3%\t8\t8 GB\t3.2 GB\t4 GB\t1 GB\t500 MB\n"
    )
    rows = grr.parse_trace(t)
    assert len(rows) == 1
    r = rows[0]
    assert r["process"] == "MIRAGE:PRE:CONVERT_IMAGE"
    assert r["tag"] == "P001"
    assert r["realtime_s"] == 600.0
    assert r["peak_rss_b"] == round(3.2 * 1024**3, 1)
    assert r["cpu_pct"] == 142.3
    assert r["exit"] == "0"


def test_parse_size_log(tmp_path):
    grr = _load()
    p = tmp_path / "input_sizes.csv"
    p.write_text(
        "process,sample_id,filename,bytes\n"
        "MIRAGE:PRE:CONVERT_IMAGE,P001,a.tiff,100\n"
        "MIRAGE:PRE:CONVERT_IMAGE,P001,b.tiff,50\n"
        "STUB,P001,stub,0\n"
    )
    m = grr.parse_size_log(p)
    assert m[("MIRAGE:PRE:CONVERT_IMAGE", "P001")] == 150


def test_rollup_by_process():
    grr = _load()
    rows = [
        {"process": "A", "tag": "P1", "exit": "0", "realtime_s": 10.0, "cpu_pct": 100.0,
         "peak_rss_b": 200.0, "peak_vmem_b": 300.0, "rchar_b": 5.0, "wchar_b": 2.0},
        {"process": "A", "tag": "P2", "exit": "0", "realtime_s": 30.0, "cpu_pct": 150.0,
         "peak_rss_b": 400.0, "peak_vmem_b": 500.0, "rchar_b": 7.0, "wchar_b": 1.0},
    ]
    roll = {r["process"]: r for r in grr.rollup_by_process(rows)}
    a = roll["A"]
    assert a["n_tasks"] == 2
    assert a["realtime_total_s"] == 40.0
    assert a["realtime_mean_s"] == 20.0
    assert a["peak_rss_max_b"] == 400.0
    assert a["cpu_max_pct"] == 150.0


def test_join_size_exact_and_fallback():
    grr = _load()
    trace = [
        {"process": "A", "tag": "P001", "realtime_s": 1.0, "peak_rss_b": 10.0},
        {"process": "A", "tag": "P001_slideX", "realtime_s": 2.0, "peak_rss_b": 20.0},
    ]
    size = {("A", "P001"): 999}
    joined = grr.join_size(trace, size)
    assert joined[0]["input_bytes"] == 999          # exact (process, tag)
    assert joined[1]["input_bytes"] == 999          # fallback: same process, sample prefix


def test_cli_writes_report(tmp_path):
    trace = tmp_path / "trace.txt"
    trace.write_text(
        "task_id\tprocess\ttag\tname\tstatus\texit\tsubmit\tstart\tcomplete\t"
        "duration\trealtime\t%cpu\tcpus\tmemory\tpeak_rss\tpeak_vmem\trchar\twchar\n"
        "1\tMIRAGE:PRE:CONVERT_IMAGE\tP001\tn\tCOMPLETED\t0\t-\t-\t-\t"
        "12m\t10m\t142%\t8\t8 GB\t3.2 GB\t4 GB\t1 GB\t500 MB\n"
        "2\tMIRAGE:REG:REGISTER\tP001\tn\tFAILED\t1\t-\t-\t-\t"
        "1h\t1h\t90%\t8\t8 GB\t7 GB\t9 GB\t2 GB\t1 GB\n"
    )
    size = tmp_path / "input_sizes.csv"
    size.write_text(
        "process,sample_id,filename,bytes\n"
        "MIRAGE:PRE:CONVERT_IMAGE,P001,a.tiff,1073741824\n"
    )
    out = tmp_path / "resource.html"
    r = subprocess.run([
        sys.executable, str(SCRIPT),
        "--trace", str(trace), "--size-log", str(size), "--output", str(out),
    ], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    html = out.read_text()
    assert "Resource" in html
    assert "MIRAGE:PRE:CONVERT_IMAGE" in html
    assert "MIRAGE:REG:REGISTER" in html
    assert "Retries" in html or "Failures" in html


def test_cli_missing_inputs_is_graceful(tmp_path):
    out = tmp_path / "resource.html"
    r = subprocess.run([
        sys.executable, str(SCRIPT),
        "--trace", str(tmp_path / "nope.txt"),
        "--size-log", str(tmp_path / "nope.csv"),
        "--output", str(out),
    ], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.exists()
    assert "not available" in out.read_text().lower()
