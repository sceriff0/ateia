"""ASHLAR was a benchmark baseline, not a production backend. v1.0.0 ships two:
valis and tiled. It survives on the `benchmarking` branch, which is the only place it
was ever used.

This guard is deliberately NARROW -- it checks the backend's own files and the
registration_method enum, NOT every occurrence of the word. Prose describing mcmicro
applying BaSiC profiles inside ASHLAR, and the provenance note on tile_residual's
correlation kernel, are correct and must survive; a blanket grep would delete them.

The nextflow.config check goes through `tests.nfmodel.strip_comments` rather than
raw text, per this repo's rule that a guard asserts against the model. That is not
ceremony here: the model view separates "the parameter is DECLARED" from "the string
appears in a comment", so the two get their own assertions and their own messages.
Both must be clean -- a leftover comment naming a parameter the schema now rejects is
its own defect -- but only the first is a broken pipeline.

Every check below whose passing answer is "zero" is paired with a positive assertion
on the same input, so an empty or mis-rooted scan fails loudly instead of passing
vacuously.
"""

import json
from pathlib import Path

from tests.nfmodel import strip_comments

REPO = Path(__file__).resolve().parent.parent

GONE = [
    "bin/ashlar_retile.py",
    "bin/ashlar_solve.py",
    "modules/local/ashlar_retile.nf",
    "modules/local/ashlar_solve.nf",
    "subworkflows/local/adapters/ashlar_adapter.nf",
    "tests/modules/ashlar_retile.nf.test",
    "tests/modules/ashlar_solve.nf.test",
    "tests/subworkflows/local/adapters/ashlar_adapter.nf.test",
    "tests/test_ashlar_retile.py",
    "tests/test_ashlar_retile_pixel_size.py",
    "tests/test_ashlar_solve.py",
]

# Surviving siblings of the deleted paths, one per directory GONE reaches into. They
# prove REPO points at a real checkout: without them "nothing is present" and "the
# root is wrong" are the same passing result.
STILL_HERE = [
    "bin/tiled_stitch.py",
    "modules/local/tiled_stitch.nf",
    "subworkflows/local/adapters/tiled_adapter.nf",
    "tests/modules/warp_seg_qc.nf.test",
    "tests/subworkflows/local/adapters/tiled_adapter.nf.test",
    "tests/test_slide_io_seam.py",
]


def test_ashlar_backend_files_are_deleted():
    missing_control = [p for p in STILL_HERE if not (REPO / p).exists()]
    assert not missing_control, (
        f"the control paths are gone too -- REPO ({REPO}) is not a mirage checkout, so "
        f"this test's 'nothing present' answer means nothing: {missing_control}"
    )
    still_here = [p for p in GONE if (REPO / p).exists()]
    assert not still_here, f"ashlar backend file(s) still present: {still_here}"


def test_registration_method_enum_is_two_backends():
    schema = json.loads((REPO / "nextflow_schema.json").read_text())
    found = []

    def walk(node):
        if not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict) and "registration_method" in props:
            found.append(props["registration_method"].get("enum"))
        for v in node.values():
            walk(v)

    walk(schema)
    assert found, "registration_method not found in nextflow_schema.json at all"
    for enum in found:
        assert enum == ["valis", "tiled"], f"expected two backends, got {enum}"


def test_no_reg_ashlar_params_remain():
    raw = (REPO / "nextflow.config").read_text()
    code = strip_comments(raw)
    # Proof the stripped view still holds the params block: a sibling STARE parameter
    # that must survive. Without it, a strip that blanked the file would pass.
    assert "reg_tiled_tile" in code, (
        "reg_tiled_tile is missing from the comment-stripped nextflow.config -- the "
        "model view is empty or wrong, so the absence check below proves nothing"
    )
    assert "reg_ashlar_" not in code, "reg_ashlar_* params still DECLARED"
    assert "reg_ashlar_" not in raw, (
        "reg_ashlar_* no longer declared but still named in a nextflow.config comment; "
        "it documents a parameter the schema now rejects at launch"
    )

    schema_text = (REPO / "nextflow_schema.json").read_text()
    assert "reg_tiled_tile" in schema_text, "nextflow_schema.json read back empty or wrong"
    assert "reg_ashlar_" not in schema_text, "reg_ashlar_* still in the schema"
