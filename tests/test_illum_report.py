import sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
from illum.report import rank_variants, recommendation_text, write_report


def _fake(name, sb, sa, cb, ca, sec):
    return {"name": name, "metrics": {
        "seam_x_before": sb, "seam_x_after": sa, "seam_y_before": sb, "seam_y_after": sa,
        "cv_before": cb, "cv_after": ca, "seconds": sec, "peak_rss_mb": 100.0}}


def test_ranking_prefers_more_suppression():
    res = [_fake("weak", 0.5, 0.4, 0.3, 0.28, 1.0),
           _fake("strong", 0.5, 0.1, 0.3, 0.15, 2.0)]
    ranked = rank_variants(res)
    assert ranked[0]["name"] == "strong"
    assert "strong" in recommendation_text(ranked)


def test_write_report_creates_html(tmp_path):
    res = [_fake("a", 0.5, 0.2, 0.3, 0.2, 1.0)]
    out = tmp_path / "report.html"
    write_report(res, {"a": ["grid.png"]}, out)
    assert out.exists() and "<table" in out.read_text()
