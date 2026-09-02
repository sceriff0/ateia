"""The meta map is an interface. One module owns it.

Two producers used to build meta independently -- INPUT_CHECK and
READ_SEGMENTED_CHECKPOINT -- with different key sets. --start postprocessing
therefore diverged silently from a full run: SPLIT_CHANNELS emitted DAPI,CD3,CD8
instead of CD3,CD8, QUANTIFY ran twice, and postprocess.nf's .unique() kept
whichever copy ARRIVED first, so the pyramid's DAPI plane came from a
nondeterministic slide.

Both are now converted (Tasks 4.2 and 4.3): input_check.nf and
segmentation.nf's READ_SEGMENTED_CHECKPOINT both build meta through
Meta.fromSamplesheetRow / Meta.fromCheckpointRow. This guard used to be
xfail(strict=True) for exactly that reason (it FAILED -- as designed -- while
the two conversions were still outstanding); Task 4.3 removed the marker as
its completion signal, once the guard genuinely passed rather than being
deleted.
"""

import re

from tests.nfmodel import REPO_ROOT, nf_files, strip_comments_and_strings

# A map literal assigned to something called `meta`, with patient_id in it.
_META_LITERAL = re.compile(r"\bmeta\s*=\s*\[[^\]]*patient_id\s*:", re.S)

# lib/Meta.groovy IS the constructor -- its own REQUIRED_KEYS-carrying literal is
# the thing every other producer is required to call instead.
#
# subworkflows/local/adapters/valis_adapter.nf is a documented, PERMANENT
# exemption (Task 4.3 decision), not a temporary gap: its one `meta = [patient_id:
# patient_id]` (line ~63, see that file's own comment at the call site) is
# REGISTER's process-input control tuple for a fan-in process call -- consumed
# only as tuple(meta, ...)'s first field, carrying none of Meta.REQUIRED_KEYS, and
# never reaching an `emit:` or a [meta, file] sample stream (the real per-slide
# metas travel separately, as `all_metas`, already built by whichever upstream
# reader produced them). It is not a producer of the meta-map INTERFACE this
# guard polices -- forcing it through Meta.fromSamplesheetRow/fromCheckpointRow
# would mean inventing values (id, is_reference, channels, keep_channels,
# channels_count, images_count) this call site has no data to supply truthfully,
# which is the "reflexive conversion to satisfy a regex" Task 4.1's review warned
# against. If this file ever grows a SECOND `meta = [...patient_id...]` literal
# beyond that one control tuple, it must fail this guard again -- this exemption
# covers the file only because, as of Task 4.3, that one line is the file's only
# occurrence (verified: `grep -c` against the guard's own regex).
ALLOWED = {"lib/Meta.groovy", "subworkflows/local/adapters/valis_adapter.nf"}


def test_only_meta_groovy_constructs_a_meta_map():
    offenders = []
    for f in nf_files():
        rel = f.relative_to(REPO_ROOT).as_posix()
        if rel in ALLOWED:
            continue
        clean = strip_comments_and_strings(f.read_text())
        for m in _META_LITERAL.finditer(clean):
            line = clean.count("\n", 0, m.start()) + 1
            offenders.append(f"{rel}:{line}")
    assert not offenders, "meta maps built outside lib/Meta.groovy:\n  " + "\n  ".join(
        offenders
    )


def test_meta_declares_its_required_keys():
    src = (REPO_ROOT / "lib" / "Meta.groovy").read_text()
    assert "REQUIRED_KEYS" in src
    for key in (
        "patient_id",
        "id",
        "is_reference",
        "channels",
        "keep_channels",
        "channels_count",
    ):
        assert f"'{key}'" in src, f"{key} is not declared in REQUIRED_KEYS"
