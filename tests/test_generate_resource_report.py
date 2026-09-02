import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_parse_bytes_inert_set_condition_is_behavior_preserving():
    """Regression test for the inert-set-condition bug: `and` bound tighter
    than `or` in the buggy `s in {"0", "-"} and s == "-"`, which reduces, for
    every value of s, to exactly `s == "-"`. A bare "0" is a genuine
    zero-byte reading (peak_rss/peak_vmem/rchar/wchar can legitimately be 0)
    and must keep falling through to float("0") == 0.0 — only "-"
    (not-run/cached) and empty are "missing", matching parse_duration's and
    parse_percent's identical guard clause."""
    grr = _load()
    assert grr.parse_bytes("0") == 0.0
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
        {
            "process": "A",
            "tag": "P1",
            "exit": "0",
            "realtime_s": 10.0,
            "cpu_pct": 100.0,
            "peak_rss_b": 200.0,
            "peak_vmem_b": 300.0,
            "rchar_b": 5.0,
            "wchar_b": 2.0,
        },
        {
            "process": "A",
            "tag": "P2",
            "exit": "0",
            "realtime_s": 30.0,
            "cpu_pct": 150.0,
            "peak_rss_b": 400.0,
            "peak_vmem_b": 500.0,
            "rchar_b": 7.0,
            "wchar_b": 1.0,
        },
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
    assert joined[0]["input_bytes"] == 999  # exact (process, tag)
    assert joined[1]["input_bytes"] == 999  # fallback: same process, sample prefix


def test_join_size_prefix_boundary_no_false_match():
    """A size sample "P1" must not prefix-match a trace tag "P10_slide" —
    only an exact match or a "<sample>_"-bounded prefix should join."""
    grr = _load()
    trace = [
        {"process": "A", "tag": "P10_slideX", "realtime_s": 1.0, "peak_rss_b": 10.0},
        {"process": "A", "tag": "P1_slideY", "realtime_s": 2.0, "peak_rss_b": 20.0},
        {"process": "A", "tag": "P1", "realtime_s": 3.0, "peak_rss_b": 30.0},
    ]
    size = {("A", "P1"): 111}
    joined = grr.join_size(trace, size)
    by_tag = {j["tag"]: j["input_bytes"] for j in joined}
    assert by_tag["P10_slideX"] is None  # P1 must not falsely match P10_...
    assert by_tag["P1_slideY"] == 111  # legitimate "<sample>_" boundary match
    assert by_tag["P1"] == 111  # exact match


def test_rollup_by_process_exit_dash_not_counted_as_failure():
    """A trace row with exit="-" (cached/aborted/not-run) must not be
    counted as a failure, either in the per-process rollup or the run-level
    failure count / Retries & Failures section."""
    grr = _load()
    rows = [
        {
            "process": "A",
            "tag": "P1",
            "exit": "-",
            "realtime_s": 1.0,
            "cpu_pct": None,
            "peak_rss_b": None,
            "peak_vmem_b": None,
            "rchar_b": None,
            "wchar_b": None,
        },
        {
            "process": "A",
            "tag": "P2",
            "exit": "0",
            "realtime_s": 1.0,
            "cpu_pct": None,
            "peak_rss_b": None,
            "peak_vmem_b": None,
            "rchar_b": None,
            "wchar_b": None,
        },
    ]
    roll = {r["process"]: r for r in grr.rollup_by_process(rows)}
    assert roll["A"]["n_failed"] == 0


def test_build_html_exit_dash_excluded_from_failures():
    grr = _load()
    trace_rows = [
        {
            "process": "A",
            "tag": "P1",
            "status": "ABORTED",
            "exit": "-",
            "realtime_s": 1.0,
            "peak_rss_b": None,
            "peak_vmem_b": None,
            "rchar_b": None,
            "wchar_b": None,
            "cpu_pct": None,
            "duration_s": None,
        },
    ]
    html_out = grr.build_html(trace_rows, {}, "ts")
    assert "<tr><th>Failed/non-zero exit</th><td>0</td></tr>" in html_out
    assert "No failed or non-zero-exit tasks." in html_out


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
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--trace",
            str(trace),
            "--size-log",
            str(size),
            "--output",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    html = out.read_text()
    assert "Resource" in html
    assert "MIRAGE:PRE:CONVERT_IMAGE" in html
    assert "MIRAGE:REG:REGISTER" in html
    assert "Retries" in html or "Failures" in html


def test_cli_missing_inputs_is_graceful(tmp_path):
    out = tmp_path / "resource.html"
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--trace",
            str(tmp_path / "nope.txt"),
            "--size-log",
            str(tmp_path / "nope.csv"),
            "--output",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert out.exists()
    assert "not available" in out.read_text().lower()


def test_build_html_keeps_zero_byte_matched_input(tmp_path):
    grr = _load()
    trace_rows = [
        {
            "process": "A",
            "tag": "P001",
            "exit": "0",
            "realtime_s": 1.0,
            "peak_rss_b": 100.0,
            "peak_vmem_b": None,
            "rchar_b": None,
            "wchar_b": None,
            "cpu_pct": None,
            "duration_s": None,
        },
    ]
    size_map = {("A", "P001"): 0}

    html = grr.build_html(trace_rows, size_map, "ts")

    assert isinstance(html, str)

    section_marker = "<h2>Resource vs Input Size</h2>"
    assert section_marker in html
    section = html[html.index(section_marker) :]
    section = section[: section.index("</section>")]

    # The zero-byte but matched row must still appear in the section (not
    # dropped as "unmatched"), and the ratio cell must be empty (N/A), not a
    # ZeroDivisionError or crash.
    assert "A" in section
    assert "P001" in section
    assert "<td></td></tr>" in section


def test_build_html_escapes_html_special_process_and_tag():
    """An HTML-special process/tag value (e.g. from an attacker-influenced
    CSV/trace field) must be escaped, never injected raw into the report."""
    grr = _load()
    trace_rows = [
        {
            "process": "A & B",
            "tag": "<b>P001</b>",
            "status": "COMPLETED",
            "exit": "0",
            "realtime_s": 1.0,
            "peak_rss_b": 10.0,
            "peak_vmem_b": None,
            "rchar_b": None,
            "wchar_b": None,
            "cpu_pct": None,
            "duration_s": None,
        },
    ]
    html_out = grr.build_html(trace_rows, {}, "ts")
    assert "<b>P001</b>" not in html_out
    assert "&lt;b&gt;P001&lt;/b&gt;" in html_out
    assert "A & B" not in html_out
    assert "A &amp; B" in html_out


def test_build_html_escapes_failing_task_status_and_exit():
    grr = _load()
    trace_rows = [
        {
            "process": "A",
            "tag": "P1",
            "status": "<i>FAILED</i>",
            "exit": "1 & 2",
            "realtime_s": 1.0,
            "peak_rss_b": None,
            "peak_vmem_b": None,
            "rchar_b": None,
            "wchar_b": None,
            "cpu_pct": None,
            "duration_s": None,
        },
    ]
    html_out = grr.build_html(trace_rows, {}, "ts")
    assert "<i>FAILED</i>" not in html_out
    assert "&lt;i&gt;FAILED&lt;/i&gt;" in html_out
    assert "1 & 2" not in html_out
    assert "1 &amp; 2" in html_out


def test_build_html_cpu_max_pct_has_percent_suffix():
    grr = _load()
    trace_rows = [
        {
            "process": "A",
            "tag": "P1",
            "status": "COMPLETED",
            "exit": "0",
            "realtime_s": 1.0,
            "peak_rss_b": None,
            "peak_vmem_b": None,
            "rchar_b": None,
            "wchar_b": None,
            "cpu_pct": 87.5,
            "duration_s": None,
        },
    ]
    html_out = grr.build_html(trace_rows, {}, "ts")
    section_marker = "<h2>Per-Process Resource Rollup</h2>"
    section = html_out[html_out.index(section_marker) :]
    section = section[: section.index("</section>")]
    assert "87.5%" in section


def test_parse_trace_reads_requested_memory_and_cpus():
    """The trace's `memory` column is the REQUEST. Without it there is no headroom
    to plot -- only observed peak_rss, which cannot show over-provisioning."""
    grr = _load()
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as d:
        t = _P(d) / "trace.txt"
        t.write_text(
            "task_id\tprocess\ttag\tname\tstatus\texit\tsubmit\tstart\tcomplete\t"
            "duration\trealtime\t%cpu\tcpus\tmemory\tpeak_rss\tpeak_vmem\trchar\twchar\n"
            "1\tA\tP001\tn\tCOMPLETED\t0\t-\t-\t-\t12m\t10m\t142%\t8\t100 GB\t3.2 GB\t4 GB\t1 GB\t500 MB\n"
            "2\tA\tP002\tn\tCOMPLETED\t0\t-\t-\t-\t12m\t10m\t142%\t-\t-\t3.2 GB\t4 GB\t1 GB\t500 MB\n"
        )
        rows = grr.parse_trace(t)

    assert rows[0]["memory_b"] == 100 * 1024**3
    assert rows[0]["cpus"] == 8
    assert rows[1]["memory_b"] is None  # "-" is missing, not zero
    assert rows[1]["cpus"] is None


def test_rollup_carries_the_max_request_and_the_cost_of_failures():
    grr = _load()
    rows = [
        # succeeded: 100 GB requested for 1 h
        {
            "process": "A",
            "tag": "P1",
            "exit": "0",
            "status": "COMPLETED",
            "realtime_s": 3600.0,
            "cpu_pct": None,
            "peak_rss_b": 50 * 1024**3,
            "peak_vmem_b": None,
            "rchar_b": None,
            "wchar_b": None,
            "memory_b": 100 * 1024**3,
            "cpus": 8,
        },
        # failed after 30 min holding a 200 GB reservation -> 100 GB.h thrown away
        {
            "process": "A",
            "tag": "P1",
            "exit": "137",
            "status": "FAILED",
            "realtime_s": 1800.0,
            "cpu_pct": None,
            "peak_rss_b": 190 * 1024**3,
            "peak_vmem_b": None,
            "rchar_b": None,
            "wchar_b": None,
            "memory_b": 200 * 1024**3,
            "cpus": 8,
        },
    ]
    a = {r["process"]: r for r in grr.rollup_by_process(rows)}["A"]

    assert a["n_failed"] == 1
    assert a["mem_req_max_b"] == 200 * 1024**3
    assert a["failed_realtime_s"] == 1800.0
    assert a["failed_gb_h"] == pytest.approx(100.0, rel=1e-6)


def test_failed_gb_h_falls_back_to_observed_rss_when_no_request_is_recorded():
    grr = _load()
    rows = [
        {
            "process": "A",
            "tag": "P1",
            "exit": "1",
            "status": "FAILED",
            "realtime_s": 7200.0,
            "cpu_pct": None,
            "peak_rss_b": 10 * 1024**3,
            "peak_vmem_b": None,
            "rchar_b": None,
            "wchar_b": None,
            "memory_b": None,
            "cpus": None,
        },
    ]
    a = {r["process"]: r for r in grr.rollup_by_process(rows)}["A"]
    assert a["failed_gb_h"] == pytest.approx(20.0, rel=1e-6)
