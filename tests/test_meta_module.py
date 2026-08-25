"""The meta map is an interface. One module owns it.

Two producers used to build meta independently -- INPUT_CHECK and
READ_SEGMENTED_CHECKPOINT -- with different key sets. --start postprocessing
therefore diverged silently from a full run: SPLIT_CHANNELS emitted DAPI,CD3,CD8
instead of CD3,CD8, QUANTIFY ran twice, and postprocess.nf's .unique() kept
whichever copy ARRIVED first, so the pyramid's DAPI plane came from a
nondeterministic slide.

test_only_meta_groovy_constructs_a_meta_map is xfail(strict=True) here: it will
FAIL until Tasks 4.2/4.3 convert the two remaining producers (currently
subworkflows/local/segmentation.nf and subworkflows/local/adapters/valis_adapter.nf
-- see the task-4.1 report for how that differs from the brief's stated
"input_check.nf and segmentation.nf"). strict=True means an unexpected PASS is
itself reported as a failure, so this cannot rot into a permanently-ignored
test. Task 4.3 removes the marker as its completion signal.
"""
import re

import pytest

from tests.nfmodel import REPO_ROOT, nf_files, strip_comments_and_strings

# A map literal assigned to something called `meta`, with patient_id in it.
_META_LITERAL = re.compile(r"\bmeta\s*=\s*\[[^\]]*patient_id\s*:", re.S)

ALLOWED = {"lib/Meta.groovy"}


@pytest.mark.xfail(strict=True, reason="producers converted in Tasks 4.2/4.3; remove this marker there")
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
    assert not offenders, (
        "meta maps built outside lib/Meta.groovy:\n  " + "\n  ".join(offenders)
    )


def test_meta_declares_its_required_keys():
    src = (REPO_ROOT / "lib" / "Meta.groovy").read_text()
    assert "REQUIRED_KEYS" in src
    for key in ("patient_id", "id", "is_reference", "channels",
                "keep_channels", "channels_count"):
        assert f"'{key}'" in src, f"{key} is not declared in REQUIRED_KEYS"
