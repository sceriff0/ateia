"""Which channels BaSiC skips, and how the FOV grid is counted.

Replaces the old ``test_preprocess_overlap.py``. That file pinned the ``--overlap``
knob, which is gone: overlap only ever changed the FOV *count* used for fitting
(extraction was always non-overlapping), nothing in the pipeline set it to anything
but 0, and it is not a parameter any more. ``count_fovs`` is now plain
``ceil(size / fov)`` -- identical arithmetic to the old formula at overlap=0, which
``test_fov_grid_covers_image`` pins.

The substantive test here is the skip rule. ``_process_single_channel_from_stack``
used to test ``"DAPI" in channel_name.upper()`` directly, so a panel whose fiducial
is CELLTOX got that channel BaSiC-corrected while a DAPI panel did not -- a silent
divergence in the channel that drives both registration and segmentation. The
decision now goes through ``utils.metadata.is_nuclear``, the same rule
``convert_image.py`` and ``split_multichannel.py`` use.
"""

import sys
import types

try:
    import basicpy  # noqa: F401
except ImportError:
    # basicpy isn't installed in every local dev environment, but
    # bin/preprocess.py imports it eagerly at module scope. Neither the tiling
    # math nor the skip decision touches BaSiC, so stub just enough of the
    # module surface to import it. In CI/containers this stub is never used.
    stub = types.ModuleType("basicpy")

    class _StubBaSiC:
        def __init__(self, *args, **kwargs):
            pass

    stub.BaSiC = _StubBaSiC
    stub.__version__ = "0.0.0-stub"
    sys.modules["basicpy"] = stub

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from bin import preprocess  # noqa: E402
from bin.preprocess import count_fovs  # noqa: E402


def test_fov_grid_covers_image():
    """The FOV grid must cover the image, with a partial row/column when it
    does not divide evenly. These are the values the pre-removal formula
    produced at overlap=0."""
    assert count_fovs((4000, 4000), (1950, 1950)) == (3, 3)
    assert count_fovs((3900, 3900), (1950, 1950)) == (2, 2)  # exact fit, no extra tile
    assert count_fovs((1000, 1000), (1950, 1950)) == (1, 1)  # smaller than one FOV


def test_fov_size_must_be_positive():
    with pytest.raises(ValueError):
        count_fovs((4000, 4000), (0, 1950))


def _run_channel(monkeypatch, name, skip_nuclear, markers):
    """Run one channel through the worker, reporting whether BaSiC was applied.

    apply_basic_correction is stubbed so the test exercises the skip DECISION
    without needing a working BaSiC (or paying for a real fit).
    """
    monkeypatch.setattr(
        preprocess,
        "apply_basic_correction",
        lambda image, fov_size=None: (image + 1, object()),
    )
    image = np.zeros((10, 10), dtype=np.uint16)
    _, out, was_corrected = preprocess._process_single_channel_from_stack(
        image, 0, name, (1950, 1950), skip_nuclear, markers
    )
    # was_corrected must agree with what actually happened to the pixels.
    assert was_corrected == bool(out.max())
    return was_corrected


@pytest.mark.parametrize(
    "markers, channel, expected_corrected",
    [
        # The configured fiducial is skipped whatever it is called...
        (["DAPI", "CELLTOX"], "DAPI", False),
        (["DAPI", "CELLTOX"], "CELLTOX", False),
        # ...including the substring spellings is_nuclear accepts.
        (["DAPI", "CELLTOX"], "DAPI_nuclear", False),
        # A marker NOT in the configured list is an ordinary channel. This is the
        # case the hardcoded "DAPI" test got wrong in both directions.
        (["CELLTOX"], "DAPI", True),
        (["DAPI"], "CELLTOX", True),
        # Ordinary markers are always corrected.
        (["DAPI", "CELLTOX"], "PANCK", True),
    ],
)
def test_skip_nuclear_follows_configured_markers(
    monkeypatch, markers, channel, expected_corrected
):
    assert (
        _run_channel(monkeypatch, channel, True, markers) is expected_corrected
    ), f"channel {channel!r} with markers {markers}"


def test_skip_nuclear_false_corrects_every_channel(monkeypatch):
    """With the switch off, even the fiducial goes through BaSiC."""
    for channel in ("DAPI", "CELLTOX", "PANCK"):
        assert _run_channel(monkeypatch, channel, False, ["DAPI", "CELLTOX"]) is True
