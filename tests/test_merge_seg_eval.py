import csv, json, subprocess, sys
from pathlib import Path

def test_merge_two(tmp_path):
    for pid, qs in [("p1", 0.5), ("p2", 0.7)]:
        (tmp_path / f"{pid}.json").write_text(json.dumps(
            {"id": pid, "QualityScore": qs,
             "metrics": {"Matched Cell": {"NumberOfCellsPer100SquareMicrons": 1.0}}}))
    out = tmp_path / "segmentation_metrics.csv"
    subprocess.run([sys.executable, "bin/merge_seg_eval.py",
                    "--inputs", str(tmp_path/"p1.json"), str(tmp_path/"p2.json"),
                    "--out", str(out)], check=True)
    rows = list(csv.DictReader(out.open()))
    assert {r["id"] for r in rows} == {"p1", "p2"}
    assert any(r["QualityScore"] == "0.7" for r in rows)
