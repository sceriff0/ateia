"""The intrinsic-TRE report must describe the mesh it claims to describe.

`bin/tiled_solve.py` gates control points on confidence and range before laying them on the
mesh, but built its `--out-tre` report from *every* control point including the rejected ones.
A rejected point's `tre` is not a conservative over-estimate of a real misalignment -- the
correlation peak is an artefact by construction, so the number is about nothing. Summarising
over it makes the reg_qc heatmap disagree with the mesh underneath it.

These tests pin the contract: every record says whether it was accepted, the percentile summary
covers accepted records only, and the spatial heatmap still carries every tile so QC can show
*where* points were dropped.

Backward compatibility: a record with no `accepted` key counts as accepted -- the same legacy
contract `_accept` already applies to a control point with no `error` key.
"""

import json
import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
)
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "utils"
    ),
)

pytest.importorskip("numpy")

import tiled_solve  # noqa: E402
from tre_report import build_tre_report  # noqa: E402


def _rec(ix, iy, tre, accepted=None):
    r = {
        "ix": ix,
        "iy": iy,
        "cx": float(ix * 10),
        "cy": float(iy * 10),
        "tre_rigid": tre,
    }
    if accepted is not None:
        r["accepted"] = accepted
    return r


def test_rigid_tre_percentiles_exclude_rejected_records():
    """A rejected point's bogus tre must not enter the summary the QC report prints."""
    records = [
        _rec(0, 0, 1.0, accepted=True),
        _rec(1, 0, 3.0, accepted=True),
        _rec(2, 0, 131.5, accepted=False),  # the measured section-edge artefact
    ]

    report = build_tre_report(0.5, 100, records, mesh_refined=True)

    assert report["rigid_tre_px"]["max"] == 3.0
    assert report["rigid_tre_px"]["mean"] == 2.0


def test_rigid_tre_percentiles_over_only_rejected_records_report_nothing():
    """All-rejected must not silently fall back to summarising the rejects."""
    records = [_rec(0, 0, 99.0, accepted=False), _rec(1, 0, 131.5, accepted=False)]

    report = build_tre_report(0.5, 100, records, mesh_refined=False)

    assert report["rigid_tre_px"]["max"] is None
    assert report["n_accepted"] == 0
    assert report["n_rejected"] == 2


def test_report_counts_accepted_and_rejected():
    """The summary must be self-describing, so a number that moved across this change explains itself."""
    records = [
        _rec(0, 0, 1.0, accepted=True),
        _rec(1, 0, 2.0, accepted=True),
        _rec(2, 0, 131.5, accepted=False),
    ]

    report = build_tre_report(0.5, 100, records, mesh_refined=True)

    assert report["n_tiles"] == 3
    assert report["n_accepted"] == 2
    assert report["n_rejected"] == 1


def test_records_with_no_accepted_key_are_all_counted():
    """Legacy contract: a report built before this change must summarise exactly as it did."""
    records = [_rec(0, 0, 1.0), _rec(1, 0, 3.0)]

    report = build_tre_report(0.5, 100, records, mesh_refined=True)

    assert report["rigid_tre_px"]["max"] == 3.0
    assert report["n_accepted"] == 2
    assert report["n_rejected"] == 0


def test_solve_marks_a_low_confidence_control_as_rejected_in_the_tre_report(tmp_path):
    """End to end: the point the gate drops from the mesh is the point the report calls rejected."""
    m0 = tmp_path / "m0.json"
    m0.write_text(
        json.dumps(
            {
                "M0": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                "ref_h": 512,
                "ref_w": 512,
                "ref_name": "ref",
                "coarse_tre": 0.5,
                "n_inliers": 100,
            }
        )
    )

    # Two tiles: one confident real match, one background tile whose peak is an artefact.
    good = {
        "ix": 0,
        "iy": 0,
        "cx": 0.0,
        "cy": 0.0,
        "dx": 2.0,
        "dy": -1.0,
        "tre": 2.24,
        "error": 0.04,
    }
    bad = {
        "ix": 1,
        "iy": 0,
        "cx": 100.0,
        "cy": 0.0,
        "dx": 60.0,
        "dy": 8.0,
        "tre": 60.5,
        "error": 0.9999,
    }
    (tmp_path / "ctrl_0.json").write_text(json.dumps(good))
    (tmp_path / "ctrl_1.json").write_text(json.dumps(bad))

    tre_f = tmp_path / "tre.json"
    tiled_solve.main(
        [
            "--m0",
            str(m0),
            "--controls",
            str(tmp_path / "ctrl_*.json"),
            "--gate-tre",
            "1.0",
            "--max-error",
            "0.99",
            "--moving-name",
            "mov",
            "--out-manifest",
            str(tmp_path / "manifest.json"),
            "--out-tre",
            str(tre_f),
        ]
    )

    report = json.loads(tre_f.read_text())
    tiles = {(t["ix"], t["iy"]): t for t in report["tiles"]}

    # The rejected tile stays in the spatial heatmap -- dropping it would hide where the
    # section edge is, which is the one thing the heatmap exists to show.
    assert report["n_tiles"] == 2
    assert tiles[(0, 0)]["accepted"] is True
    assert tiles[(1, 0)]["accepted"] is False


def test_solve_tre_summary_ignores_the_rejected_control(tmp_path):
    """The headline rigid TRE must not be inflated by a point the mesh never used."""
    m0 = tmp_path / "m0.json"
    m0.write_text(
        json.dumps(
            {
                "M0": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                "ref_h": 512,
                "ref_w": 512,
                "ref_name": "ref",
                "coarse_tre": 0.5,
                "n_inliers": 100,
            }
        )
    )
    (tmp_path / "ctrl_0.json").write_text(
        json.dumps(
            {
                "ix": 0,
                "iy": 0,
                "cx": 0.0,
                "cy": 0.0,
                "dx": 2.0,
                "dy": -1.0,
                "tre": 2.0,
                "error": 0.04,
            }
        )
    )
    (tmp_path / "ctrl_1.json").write_text(
        json.dumps(
            {
                "ix": 1,
                "iy": 0,
                "cx": 100.0,
                "cy": 0.0,
                "dx": 60.0,
                "dy": 8.0,
                "tre": 60.0,
                "error": 0.9999,
            }
        )
    )

    tre_f = tmp_path / "tre.json"
    tiled_solve.main(
        [
            "--m0",
            str(m0),
            "--controls",
            str(tmp_path / "ctrl_*.json"),
            "--gate-tre",
            "1.0",
            "--max-error",
            "0.99",
            "--moving-name",
            "mov",
            "--out-manifest",
            str(tmp_path / "manifest.json"),
            "--out-tre",
            str(tre_f),
        ]
    )

    report = json.loads(tre_f.read_text())

    assert report["rigid_tre_px"]["max"] == 2.0
    assert report["n_accepted"] == 1
    assert report["n_rejected"] == 1


# ---------------------------------------------------------------------------
# The QC report renders this same data. Fixing the summary while the heatmap
# beside it still colours a rejected tile by its bogus TRE would be a half-fix:
# the human reads "misregistered here" when the truth is "we dropped this tile".
# ---------------------------------------------------------------------------


def _write_tre(tmp_path, tiles, **extra):
    doc = {
        "coarse_tre_px": 0.5,
        "n_inliers": 100,
        "n_tiles": len(tiles),
        "mesh_refined": True,
        "rigid_tre_px": {"mean": 2.0, "p50": 2.0, "p90": 3.0, "max": 3.0},
        "tiles": tiles,
        "moving": "mov",
    }
    doc.update(extra)
    p = tmp_path / "mov_tre.json"
    p.write_text(json.dumps(doc))
    return p


def test_qc_summary_surfaces_the_accepted_and_rejected_counts(tmp_path):
    """A reader must be able to see that points were dropped, not just infer it."""
    import generate_qc_report as gqr

    p = _write_tre(
        tmp_path,
        [
            {
                "ix": 0,
                "iy": 0,
                "cx": 0.0,
                "cy": 0.0,
                "tre_rigid": 2.0,
                "accepted": True,
            },
            {
                "ix": 1,
                "iy": 0,
                "cx": 10.0,
                "cy": 0.0,
                "tre_rigid": 131.5,
                "accepted": False,
            },
        ],
        n_accepted=1,
        n_rejected=1,
    )

    info = gqr.parse_tiled_tre_json(str(p))

    assert info["n_accepted"] == 1
    assert info["n_rejected"] == 1


def test_heatmap_colour_scale_ignores_rejected_tiles(tmp_path):
    """One rejected tile at 131.5px must not compress every real tile into the green end."""
    import generate_qc_report as gqr

    p = _write_tre(
        tmp_path,
        [
            {
                "ix": 0,
                "iy": 0,
                "cx": 0.0,
                "cy": 0.0,
                "tre_rigid": 1.0,
                "accepted": True,
            },
            {
                "ix": 1,
                "iy": 0,
                "cx": 10.0,
                "cy": 0.0,
                "tre_rigid": 3.0,
                "accepted": True,
            },
            {
                "ix": 2,
                "iy": 0,
                "cx": 20.0,
                "cy": 0.0,
                "tre_rigid": 131.5,
                "accepted": False,
            },
        ],
    )
    svg = gqr._tiled_tre_heatmap_svg(gqr.parse_tiled_tre_json(str(p)))

    assert "max 3.00px" in svg
    assert "131.50" not in svg.split("<title>")[0]


def test_heatmap_marks_a_rejected_tile_as_rejected(tmp_path):
    """A dropped tile must not be rendered as if it were a measurement."""
    import generate_qc_report as gqr

    p = _write_tre(
        tmp_path,
        [
            {
                "ix": 0,
                "iy": 0,
                "cx": 0.0,
                "cy": 0.0,
                "tre_rigid": 1.0,
                "accepted": True,
            },
            {
                "ix": 1,
                "iy": 0,
                "cx": 10.0,
                "cy": 0.0,
                "tre_rigid": 131.5,
                "accepted": False,
            },
        ],
    )
    svg = gqr._tiled_tre_heatmap_svg(gqr.parse_tiled_tre_json(str(p)))

    assert "rejected" in svg


def test_heatmap_unchanged_when_no_tile_carries_an_accepted_key(tmp_path):
    """Legacy _tre.json must render exactly as before."""
    import generate_qc_report as gqr

    p = _write_tre(
        tmp_path,
        [
            {"ix": 0, "iy": 0, "cx": 0.0, "cy": 0.0, "tre_rigid": 1.0},
            {"ix": 1, "iy": 0, "cx": 10.0, "cy": 0.0, "tre_rigid": 4.0},
        ],
    )
    svg = gqr._tiled_tre_heatmap_svg(gqr.parse_tiled_tre_json(str(p)))

    assert "max 4.00px" in svg
    assert "rejected" not in svg


def test_summary_table_tiles_column_shows_how_many_were_used(tmp_path):
    """ "Tiles: 3" hides that only 2 of them built the mesh."""
    import generate_qc_report as gqr

    p = _write_tre(
        tmp_path,
        [
            {
                "ix": 0,
                "iy": 0,
                "cx": 0.0,
                "cy": 0.0,
                "tre_rigid": 1.0,
                "accepted": True,
            },
            {
                "ix": 1,
                "iy": 0,
                "cx": 10.0,
                "cy": 0.0,
                "tre_rigid": 3.0,
                "accepted": True,
            },
            {
                "ix": 2,
                "iy": 0,
                "cx": 20.0,
                "cy": 0.0,
                "tre_rigid": 131.5,
                "accepted": False,
            },
        ],
        n_tiles=3,
        n_accepted=2,
        n_rejected=1,
    )

    table = gqr._tiled_tre_tables([str(p)])

    assert "2 / 3" in table


def test_summary_table_tiles_column_is_a_plain_count_with_no_rejections(tmp_path):
    """No rejections must not clutter the table with a ratio that never varies."""
    import generate_qc_report as gqr

    p = _write_tre(
        tmp_path,
        [
            {"ix": 0, "iy": 0, "cx": 0.0, "cy": 0.0, "tre_rigid": 1.0},
            {"ix": 1, "iy": 0, "cx": 10.0, "cy": 0.0, "tre_rigid": 3.0},
        ],
        n_tiles=2,
    )

    table = gqr._tiled_tre_tables([str(p)])

    assert ">2<" in table
    assert "2 / 2" not in table
