"""The FOV grid arithmetic in ``bin/utils/fov_tiling.py``.

Was ``tests/test_preprocess_channel_skip.py``, which covered two separate things
against the now-deleted ``bin/preprocess.py``: the FOV grid count, and the
nuclear/fiducial skip decision made by ``_process_single_channel_from_stack``.

The skip half moved with the code it tested. The in-process BaSiC path is gone --
illumination correction runs through nf-core's BASICPY (``TILE_FOR_BASIC`` ->
``BASICPY`` -> ``APPLY_PROFILES``) -- and the skip decision is now made once, in
``bin/tile_for_basic.py``, through the same ``utils.metadata.is_nuclear`` rule. It is
covered there, against the live code, by
``tests/test_tile_for_basic.py::test_the_configured_fiducial_is_excluded_from_the_fit``,
``::test_skip_nuclear_off_corrects_every_channel`` and
``::test_a_celltox_only_panel_still_produces_a_readable_stack``, and on the applying
side by ``tests/test_apply_basic_profiles.py::test_a_celltox_fiducial_is_passed_through_untouched``.
Keeping a second copy here would have meant monkeypatching a function that no longer
exists, i.e. a test that passes without exercising anything.

What survives is the grid count, because the tiling itself survived the backend swap:
``count_fovs`` is plain ``ceil(size / fov)``, and these are the values the pre-removal
formula produced at overlap=0 (the ``--overlap`` knob is long gone; it only ever changed
the FOV *count* used for fitting, never the extraction, which was always
non-overlapping).
"""

import pytest

from bin.utils.fov_tiling import count_fovs


def test_fov_grid_covers_image():
    """The FOV grid must cover the image, with a partial row/column when it
    does not divide evenly."""
    assert count_fovs((4000, 4000), (1950, 1950)) == (3, 3)
    assert count_fovs((3900, 3900), (1950, 1950)) == (2, 2)  # exact fit, no extra tile
    assert count_fovs((1000, 1000), (1950, 1950)) == (1, 1)  # smaller than one FOV


def test_fov_size_must_be_positive():
    with pytest.raises(ValueError):
        count_fovs((4000, 4000), (0, 1950))
