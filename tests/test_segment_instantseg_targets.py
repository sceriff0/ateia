"""InstanSeg returned the SAME mask for nuclei and cells, silently.

`_extract_2d_masks` reduced InstanSeg's labelled output to a (nuclei, cell)
pair. When the output carried a single label map -- which is what
`--target nuclei` and `--target cells` produce by construction -- it returned
`single, single`, and the module docstring called that "contract-preserving".

It preserves the FILE contract (both `_nuclei_mask.tif` and `_cell_mask.tif`
get written) and destroys the DATA contract. Downstream,
Cytoplasm = Cell - Nucleus, so an identical pair makes the cytoplasm empty for
every cell in the slide: every cytoplasmic column in measurements.csv is zero
and nothing anywhere says why. There is no output that looks wrong.

Scope, checked rather than assumed: nextflow.config:284 defaults
`seg_instantseg_target` to 'all_outputs', so the DEFAULT path was never
affected -- only a run that explicitly asks for one target. That is why this
is a hard failure now rather than a config change: no shipped path relied on
the replication.

The degenerate `all_outputs` case raises too, deviating from the plan text,
which kept a warn-and-replicate branch for it. The warning does not make the
answer less wrong -- it lands in a container log nobody reads, while the
zeroed cytoplasm reaches the published measurements.csv. If a model cannot
produce two masks, the run should say so at the point it finds out.
"""

import numpy as np
import pytest
from segment_instantseg import _extract_2d_masks

SINGLE_MASK_TARGETS = ["nuclei", "cells"]


def _two_channel(y=8, x=8):
    """A (2, Y, X) output: index 0 = nuclei, index 1 = cells. The cell labels
    are deliberately a DIFFERENT array, so a test that passes because both
    happen to be equal cannot happen here."""
    nuclei = np.zeros((y, x), dtype=np.int32)
    nuclei[2:4, 2:4] = 1
    cells = np.zeros((y, x), dtype=np.int32)
    cells[1:6, 1:6] = 1
    return np.stack([nuclei, cells])


@pytest.mark.parametrize(
    "wrapping",
    [
        lambda a: a,  # (2, Y, X)
        lambda a: a[np.newaxis, ...],  # (1, 2, Y, X) -- batch axis
    ],
)
def test_all_outputs_returns_two_genuinely_different_masks(wrapping):
    nuclei, cells = _extract_2d_masks(wrapping(_two_channel()), "all_outputs")
    assert nuclei.shape == cells.shape == (8, 8)
    assert not np.array_equal(nuclei, cells), (
        "nuclei and cell masks are identical, so Cytoplasm = Cell - Nucleus is "
        "empty for every cell"
    )
    # The cytoplasm the pipeline actually computes must be non-empty.
    assert ((cells > 0) & (nuclei == 0)).any()


@pytest.mark.parametrize("target", SINGLE_MASK_TARGETS)
@pytest.mark.parametrize("shape", [(8, 8), (1, 8, 8), (1, 1, 8, 8)])
def test_an_explicit_single_target_refuses_rather_than_replicating(target, shape):
    """Every shape InstanSeg is observed to return for a single target, at
    every level of singleton wrapping the extractor strips."""
    arr = np.zeros(shape, dtype=np.int32)
    arr[..., 2:4, 2:4] = 1
    with pytest.raises(ValueError) as exc:
        _extract_2d_masks(arr, target)
    msg = str(exc.value)
    assert target in msg
    assert "all_outputs" in msg, "the error must name the target that works"


@pytest.mark.parametrize("shape", [(8, 8), (1, 8, 8), (1, 1, 8, 8)])
def test_a_degenerate_all_outputs_refuses_too(shape):
    """A model that cannot produce two masks must not be allowed to pretend it
    did. This is the branch that used to warn and replicate."""
    arr = np.zeros(shape, dtype=np.int32)
    arr[..., 2:4, 2:4] = 1
    with pytest.raises(ValueError) as exc:
        _extract_2d_masks(arr, "all_outputs")
    assert "single" in str(exc.value).lower() or "one" in str(exc.value).lower()


def test_an_unexpected_shape_still_raises_its_own_error():
    """The pre-existing guard, kept: a 4D array that does not reduce is a
    different failure from a single mask and must not be folded into it."""
    with pytest.raises(ValueError, match="Unexpected InstanSeg output shape"):
        _extract_2d_masks(np.zeros((3, 4, 8, 8), dtype=np.int32), "all_outputs")


def test_the_schema_enum_cannot_offer_a_target_the_reader_refuses():
    """Schema and reader are two statements of the same rule, in two languages,
    with nothing tying them together. Without this they drift the moment one is
    edited -- and the drift is invisible, because the schema's job is to REFUSE
    values, so an over-wide enum simply lets a launch through to fail later
    inside a segmentation task on the cluster.

    Narrowing the enum is what moves that failure to launch time; this asserts
    the narrowing is still there.
    """
    import json
    from pathlib import Path

    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "nextflow_schema.json").read_text()
    )

    def find(node):
        if isinstance(node, dict):
            if "seg_instantseg_target" in node.get("properties", {}):
                return node["properties"]["seg_instantseg_target"]
            for v in node.values():
                hit = find(v)
                if hit:
                    return hit
        return None

    entry = find(schema)
    assert entry, "seg_instantseg_target is no longer in nextflow_schema.json"
    offered = set(entry.get("enum", []))
    assert offered, "seg_instantseg_target lost its enum -- any string now launches"
    bad = offered & set(SINGLE_MASK_TARGETS)
    assert not bad, (
        f"nextflow_schema.json still offers {sorted(bad)}, which "
        "_extract_2d_masks raises on. The launch would be accepted and the run "
        "would fail inside a segmentation task instead."
    )


def test_the_rendered_command_still_reads_the_param():
    """The narrowed enum only protects anything if the param reaches the
    command. conf/modules.config's InstanSeg ext.args is the one link, and
    -stub never evaluates script:, so nothing else in the suite walks it."""
    from tests.nfmodel import strip_comments, with_name_blocks

    # The comments-blanked, STRINGS-INTACT view: the param is interpolated
    # inside a quoted flag ("--target ${params.seg_instantseg_target}"), so the
    # fully-stripped view blanks it away and this scan would find nothing --
    # a false failure that looks exactly like a real regression.
    readers = [
        b.selector
        for b in with_name_blocks()
        if "params.seg_instantseg_target" in strip_comments(b.raw_body)
    ]
    assert readers, (
        "no withName block reads params.seg_instantseg_target any more -- the "
        "schema enum and the reader's refusal are both guarding a value that "
        "no longer reaches segment_instantseg.py"
    )


def test_no_caller_ships_a_single_mask_target_by_default():
    """The reason this can be a hard failure rather than a config migration.

    Read from nextflow.config rather than restated, so the day the default
    changes to 'nuclei' this test fails instead of the users."""
    import re
    from pathlib import Path

    cfg = (Path(__file__).resolve().parents[1] / "nextflow.config").read_text()
    m = re.search(r"^\s*seg_instantseg_target\s*=\s*'([^']+)'", cfg, re.M)
    assert m, "seg_instantseg_target is no longer declared in nextflow.config"
    assert m.group(1) not in SINGLE_MASK_TARGETS, (
        f"the shipped default is now '{m.group(1)}', which raises. Either the "
        "default is wrong or _extract_2d_masks must learn to produce two masks "
        "from it -- do not soften the error to make this pass."
    )
