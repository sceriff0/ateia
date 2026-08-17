"""APPLY_PROFILES: the step nf-core's module deliberately does not provide.

``modules/nf-core/basicpy`` computes PROFILES ONLY -- mcmicro applies them downstream,
inside ASHLAR. mirage has no ASHLAR, so ``bin/apply_basic_profiles.py`` is where

    corrected = (image - darkfield) / flatfield

happens, per channel, per pseudo-FOV, followed by reassembly from the sidecar the tiling
step wrote.

Three contracts from the in-process BaSiC path have to survive the swap, and each has a
test here that fails if it does not:

  * the nuclear/fiducial channel is left alone (the deleted ``bin/preprocess.py``'s skip, whose
    comment records that an earlier version silently corrected a configured CELLTOX
    fiducial);
  * negative pixels are clipped to zero and reported in ONE aggregate line, not one line
    per channel, via ``bin/utils/validation.py``'s ``clip_negative_values``. NOTE the
    rationale has changed: ``bin/preprocess.py`` had explained this by darkfield subtraction
    pushing a pixel below zero, and the pipeline now runs BASICPY at upstream defaults
    where ``get_darkfield`` is FALSE, so that mechanism is largely gone. The contract is
    kept because downstream expects it and because this script still subtracts whatever
    darkfield it is handed -- a non-zero one when run standalone, or if the decision to
    stay on upstream defaults is ever revisited;
  * the storage cast ROUNDS before truncating, because ``.astype()`` floors and a
    one-sided -0.5 LSB bias never averages out.
"""

from __future__ import annotations

import json
import logging

import pytest

np = pytest.importorskip("numpy")
tifffile = pytest.importorskip("tifffile")

# No basicpy stub any more -- see tests/test_tile_for_basic.py. Nothing on this path
# imports basicpy since the in-process BaSiC module was deleted.
import apply_basic_profiles  # noqa: E402
import tile_for_basic  # noqa: E402


def _write_slide(path, stack, channel_names):
    tifffile.imwrite(
        str(path),
        stack,
        photometric="minisblack",
        metadata={"axes": "CYX", "Channel": {"Name": list(channel_names)}},
        ome=True,
    )


def _write_profile(path, planes):
    """Write a (C, Y, X) float profile the way /opt/main.py does.

    ``main.py`` builds ``np.array(flatfields)`` -- one 2-D plane per fitted channel --
    and hands it to ``tifffile.imwrite(..., ome=True)``. Nothing else is added, so this
    is the whole shape of the contract.
    """
    tifffile.imwrite(
        str(path),
        np.asarray(planes, dtype=np.float32),
        photometric="minisblack",
        ome=True,
    )


def _read(path):
    """Read a written slide as ``(C, Y, X)``.

    tifffile drops a singleton leading axis, so a one-channel corrected slide comes back
    2-D. That is unchanged from the in-process path -- it wrote ``(1, H, W)``
    with ``axes="CYX"`` too -- so it is normalised here rather than "fixed" in the writer.
    """
    data = tifffile.imread(str(path))
    return data[np.newaxis, ...] if data.ndim == 2 else data


def _tile(tmp_path, stack, names, fov=100, skip_nuclear=True, markers=("DAPI",)):
    slide = tmp_path / "slide.ome.tif"
    _write_slide(slide, stack, names)
    sidecar = tmp_path / "slide_tiles.json"
    manifest = tile_for_basic.tile_for_basic(
        str(slide),
        str(tmp_path / "slide_tiles.ome.tif"),
        str(sidecar),
        channel_names=list(names),
        fov_size=(fov, fov),
        skip_nuclear=skip_nuclear,
        nuclear_markers=list(markers),
    )
    return slide, sidecar, manifest


def _flat_profiles(tmp_path, manifest, flat_value, dark_value):
    th, tw = manifest["tile_shape"]
    n = len(manifest["profile_channels"])
    ffp = tmp_path / "slide_tiles-ffp.ome.tif"
    dfp = tmp_path / "slide_tiles-dfp.ome.tif"
    _write_profile(ffp, [np.full((th, tw), flat_value)] * n)
    _write_profile(dfp, [np.full((th, tw), dark_value)] * n)
    return ffp, dfp


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def test_flatfield_divides_and_darkfield_subtracts(tmp_path):
    original = np.full((2, 250, 250), 1000, dtype=np.uint16)
    slide, sidecar, manifest = _tile(tmp_path, original, ["DAPI", "PANCK"])
    ffp, dfp = _flat_profiles(tmp_path, manifest, flat_value=2.0, dark_value=100.0)

    out = tmp_path / "slide_corrected.ome.tif"
    apply_basic_profiles.apply_basic_profiles(
        str(slide), str(sidecar), str(ffp), str(dfp), str(out)
    )

    result = _read(out)
    # (1000 - 100) / 2 = 450 on the corrected channel; DAPI is the fiducial, untouched.
    assert np.all(result[1] == 450)
    assert np.all(result[0] == 1000)


def test_a_unit_flatfield_and_zero_darkfield_round_trip_the_slide_exactly(tmp_path):
    """Output-equivalence: correction with an identity profile must be the identity.

    This exercises the whole tile -> profile -> reassemble path, so a wrong tile
    position, a lost edge tile, or leaked zero padding all show up as changed pixels.
    """
    rng = np.random.default_rng(3)
    original = rng.integers(0, 5000, size=(3, 250, 330), dtype=np.uint16)
    slide, sidecar, manifest = _tile(tmp_path, original, ["DAPI", "PANCK", "SMA"])
    ffp, dfp = _flat_profiles(tmp_path, manifest, flat_value=1.0, dark_value=0.0)

    out = tmp_path / "slide_corrected.ome.tif"
    apply_basic_profiles.apply_basic_profiles(
        str(slide), str(sidecar), str(ffp), str(dfp), str(out)
    )

    assert np.array_equal(_read(out), original)


def test_each_channel_gets_its_own_profile(tmp_path):
    """A single shared profile would pass every test above; this one pins the mapping.

    The profile stack's C axis is indexed by ``profile_channels`` -- position in the tile
    stack -- not by source channel index, and the two differ whenever a fiducial is
    skipped.
    """
    original = np.full((3, 250, 250), 1200, dtype=np.uint16)
    slide, sidecar, manifest = _tile(tmp_path, original, ["DAPI", "PANCK", "SMA"])
    th, tw = manifest["tile_shape"]
    ffp = tmp_path / "slide_tiles-ffp.ome.tif"
    dfp = tmp_path / "slide_tiles-dfp.ome.tif"
    # profile_channels == [1, 2]: PANCK is divided by 2, SMA by 3.
    _write_profile(ffp, [np.full((th, tw), 2.0), np.full((th, tw), 3.0)])
    _write_profile(dfp, [np.zeros((th, tw)), np.zeros((th, tw))])

    out = tmp_path / "slide_corrected.ome.tif"
    apply_basic_profiles.apply_basic_profiles(
        str(slide), str(sidecar), str(ffp), str(dfp), str(out)
    )

    result = _read(out)
    assert np.all(result[0] == 1200)
    assert np.all(result[1] == 600)
    assert np.all(result[2] == 400)


def test_a_spatially_varying_profile_is_applied_per_tile_pixel(tmp_path):
    """The profile is a TILE-shaped field, not a scalar -- it must land tile-wise."""
    original = np.full((1, 200, 200), 1000, dtype=np.uint16)
    slide, sidecar, manifest = _tile(
        tmp_path, original, ["PANCK"], skip_nuclear=False, markers=("DAPI",)
    )
    th, tw = manifest["tile_shape"]
    flat = np.ones((th, tw), dtype=np.float32)
    flat[:, : tw // 2] = 2.0  # left half of every tile is divided by two
    ffp = tmp_path / "slide_tiles-ffp.ome.tif"
    dfp = tmp_path / "slide_tiles-dfp.ome.tif"
    _write_profile(ffp, [flat])
    _write_profile(dfp, [np.zeros((th, tw))])

    out = tmp_path / "slide_corrected.ome.tif"
    apply_basic_profiles.apply_basic_profiles(
        str(slide), str(sidecar), str(ffp), str(dfp), str(out)
    )

    result = _read(out)[0]
    # Two tiles across, each 100 wide, left 50 columns of each halved.
    assert np.all(result[:, 0:50] == 500)
    assert np.all(result[:, 50:100] == 1000)
    assert np.all(result[:, 100:150] == 500)
    assert np.all(result[:, 150:200] == 1000)


# ---------------------------------------------------------------------------
# The fiducial skip
# ---------------------------------------------------------------------------


def test_a_celltox_fiducial_is_passed_through_untouched(tmp_path):
    """The exact regression the deleted bin/preprocess.py's comment recorded.

    The skip decision is made once, at tiling time, and recorded in the sidecar; this
    asserts the applying half honours it for a panel whose fiducial is not DAPI.
    """
    rng = np.random.default_rng(11)
    original = rng.integers(1, 5000, size=(2, 250, 250), dtype=np.uint16)
    slide, sidecar, manifest = _tile(
        tmp_path, original, ["CELLTOX", "PANCK"], markers=("CELLTOX",)
    )
    ffp, dfp = _flat_profiles(tmp_path, manifest, flat_value=2.0, dark_value=0.0)

    out = tmp_path / "slide_corrected.ome.tif"
    apply_basic_profiles.apply_basic_profiles(
        str(slide), str(sidecar), str(ffp), str(dfp), str(out)
    )

    result = _read(out)
    assert np.array_equal(result[0], original[0]), "the fiducial must not be corrected"
    assert not np.array_equal(result[1], original[1])


def test_an_all_fiducial_panel_is_returned_unchanged(tmp_path):
    """A CELLTOX-only panel is a supported input: profiles get fitted and applied to nothing."""
    rng = np.random.default_rng(13)
    original = rng.integers(1, 5000, size=(1, 250, 250), dtype=np.uint16)
    slide, sidecar, manifest = _tile(
        tmp_path, original, ["CELLTOX"], markers=("CELLTOX",)
    )
    assert manifest["corrected_channels"] == []
    ffp, dfp = _flat_profiles(tmp_path, manifest, flat_value=7.0, dark_value=900.0)

    out = tmp_path / "slide_corrected.ome.tif"
    apply_basic_profiles.apply_basic_profiles(
        str(slide), str(sidecar), str(ffp), str(dfp), str(out)
    )

    assert np.array_equal(_read(out)[0], original[0])


# ---------------------------------------------------------------------------
# The negative-clip contract
# ---------------------------------------------------------------------------


def _clip_records(caplog):
    return [r for r in caplog.records if "Clipped" in r.getMessage()]


def test_negative_values_are_clipped_and_reported_in_one_aggregate_line(tmp_path, caplog):
    """Darkfield subtraction produces negatives; ONE line reports them for the whole image.

    ``clip_negative_values`` computes the percentage against the size of the array it is
    given. Calling it per channel would emit C lines whose percentages are each computed
    against one channel -- same pixels, wrong observable output. It is called once, on
    the whole stack, so the numbers below are whole-image numbers.
    """
    original = np.zeros((3, 250, 250), dtype=np.uint16)
    original[1] = 10  # channel 1 goes negative after a darkfield of 100
    original[2] = 10  # so does channel 2 -- two channels, still one line
    slide, sidecar, manifest = _tile(tmp_path, original, ["DAPI", "PANCK", "SMA"])
    ffp, dfp = _flat_profiles(tmp_path, manifest, flat_value=1.0, dark_value=100.0)

    out = tmp_path / "slide_corrected.ome.tif"
    with caplog.at_level(logging.INFO):
        apply_basic_profiles.apply_basic_profiles(
            str(slide), str(sidecar), str(ffp), str(dfp), str(out)
        )

    records = _clip_records(caplog)
    assert len(records) == 1, f"expected one aggregate clip line, got {len(records)}"

    message = records[0].getMessage()
    negatives = 2 * 250 * 250
    total = 3 * 250 * 250
    assert f"Clipped {negatives} negative pixels" in message
    assert f"({negatives / total * 100:.4f}%)" in message

    assert np.all(_read(out) == 0)


def test_no_clip_line_when_nothing_is_negative(tmp_path, caplog):
    original = np.full((2, 250, 250), 1000, dtype=np.uint16)
    slide, sidecar, manifest = _tile(tmp_path, original, ["DAPI", "PANCK"])
    ffp, dfp = _flat_profiles(tmp_path, manifest, flat_value=2.0, dark_value=0.0)

    with caplog.at_level(logging.INFO):
        apply_basic_profiles.apply_basic_profiles(
            str(slide),
            str(sidecar),
            str(ffp),
            str(dfp),
            str(tmp_path / "slide_corrected.ome.tif"),
        )

    assert _clip_records(caplog) == []


# ---------------------------------------------------------------------------
# The storage dtype rule
# ---------------------------------------------------------------------------


def test_the_storage_cast_rounds_rather_than_flooring(tmp_path):
    """``.astype()`` truncates toward zero -- a one-sided -0.5 LSB bias on every pixel.

    1000 / 3 = 333.33 -> 333, and 2000 / 3 = 666.67 -> 667. A flooring cast gives 666 for
    the second, which is the whole point.
    """
    original = np.zeros((2, 250, 250), dtype=np.uint16)
    original[0] = 1000
    original[1] = 2000
    slide, sidecar, manifest = _tile(
        tmp_path, original, ["PANCK", "SMA"], skip_nuclear=False
    )
    ffp, dfp = _flat_profiles(tmp_path, manifest, flat_value=3.0, dark_value=0.0)

    out = tmp_path / "slide_corrected.ome.tif"
    apply_basic_profiles.apply_basic_profiles(
        str(slide), str(sidecar), str(ffp), str(dfp), str(out)
    )

    result = _read(out)
    assert np.all(result[0] == 333)
    assert np.all(result[1] == 667)


def test_the_storage_cast_clips_before_rounding_so_nothing_wraps(tmp_path):
    """65535.6 rounds to 65536, which wraps to 0 in uint16 unless the clip comes first."""
    original = np.full((1, 250, 250), 60000, dtype=np.uint16)
    slide, sidecar, manifest = _tile(
        tmp_path, original, ["PANCK"], skip_nuclear=False
    )
    ffp, dfp = _flat_profiles(tmp_path, manifest, flat_value=0.5, dark_value=0.0)

    out = tmp_path / "slide_corrected.ome.tif"
    apply_basic_profiles.apply_basic_profiles(
        str(slide), str(sidecar), str(ffp), str(dfp), str(out)
    )

    result = _read(out)
    assert result.dtype == np.uint16
    assert np.all(result == 65535)


# ---------------------------------------------------------------------------
# Output metadata and sidecar validation
# ---------------------------------------------------------------------------


def test_the_written_slide_keeps_channel_names_and_the_pixel_size(tmp_path):
    original = np.full((2, 250, 250), 1000, dtype=np.uint16)
    slide, sidecar, manifest = _tile(tmp_path, original, ["DAPI", "PANCK"])
    ffp, dfp = _flat_profiles(tmp_path, manifest, flat_value=1.0, dark_value=0.0)

    out = tmp_path / "slide_corrected.ome.tif"
    apply_basic_profiles.apply_basic_profiles(
        str(slide), str(sidecar), str(ffp), str(dfp), str(out), pixel_size=0.5
    )

    with tifffile.TiffFile(str(out)) as tif:
        ome = tif.ome_metadata
    assert 'Name="DAPI"' in ome
    assert 'Name="PANCK"' in ome
    assert 'PhysicalSizeX="0.5"' in ome


def test_an_unknown_sidecar_version_is_refused(tmp_path):
    original = np.full((2, 250, 250), 1000, dtype=np.uint16)
    slide, sidecar, manifest = _tile(tmp_path, original, ["DAPI", "PANCK"])
    ffp, dfp = _flat_profiles(tmp_path, manifest, flat_value=1.0, dark_value=0.0)

    payload = json.loads(sidecar.read_text())
    payload["format_version"] = 99
    sidecar.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="format_version"):
        apply_basic_profiles.apply_basic_profiles(
            str(slide),
            str(sidecar),
            str(ffp),
            str(dfp),
            str(tmp_path / "slide_corrected.ome.tif"),
        )


def test_a_profile_stack_that_does_not_match_the_sidecar_is_refused(tmp_path):
    """A silent broadcast against the wrong number of profiles is the failure to avoid."""
    original = np.full((3, 250, 250), 1000, dtype=np.uint16)
    slide, sidecar, manifest = _tile(tmp_path, original, ["DAPI", "PANCK", "SMA"])
    th, tw = manifest["tile_shape"]
    ffp = tmp_path / "slide_tiles-ffp.ome.tif"
    dfp = tmp_path / "slide_tiles-dfp.ome.tif"
    _write_profile(ffp, [np.ones((th, tw))])  # one profile, two fitted channels
    _write_profile(dfp, [np.zeros((th, tw))])

    with pytest.raises(ValueError, match="profile"):
        apply_basic_profiles.apply_basic_profiles(
            str(slide),
            str(sidecar),
            str(ffp),
            str(dfp),
            str(tmp_path / "slide_corrected.ome.tif"),
        )


def test_an_all_zero_darkfield_is_reported_rather_than_pretended(tmp_path, caplog):
    """At upstream defaults ``get_darkfield`` is FALSE, so this is the SHIPPED case.

    basicpy leaves ``darkfield`` at its zero initialisation and ``fit`` resizes it to the
    tile shape, so ``*-dfp.ome.tif`` is present, correctly shaped, and all zeros -- it is
    not absent and it is not the wrong size. The correction is then flatfield-only, and
    the log has to say so rather than implying an additive offset was removed.
    """
    original = np.full((2, 250, 250), 1000, dtype=np.uint16)
    slide, sidecar, manifest = _tile(tmp_path, original, ["DAPI", "PANCK"])
    ffp, dfp = _flat_profiles(tmp_path, manifest, flat_value=2.0, dark_value=0.0)

    out = tmp_path / "slide_corrected.ome.tif"
    with caplog.at_level(logging.INFO):
        apply_basic_profiles.apply_basic_profiles(
            str(slide), str(sidecar), str(ffp), str(dfp), str(out)
        )

    messages = [r.getMessage() for r in caplog.records]
    assert any("no darkfield" in m.lower() for m in messages), messages
    assert np.all(_read(out)[1] == 500)


def test_an_absent_darkfield_file_is_treated_as_zero(tmp_path, caplog):
    """Handled, not assumed. The vendored module always writes the file, but the script is
    hand-runnable and a future upstream that omits it must not produce a crash or, worse,
    a silent misread."""
    original = np.full((2, 250, 250), 1000, dtype=np.uint16)
    slide, sidecar, manifest = _tile(tmp_path, original, ["DAPI", "PANCK"])
    ffp, _dfp = _flat_profiles(tmp_path, manifest, flat_value=2.0, dark_value=0.0)

    out = tmp_path / "slide_corrected.ome.tif"
    with caplog.at_level(logging.INFO):
        apply_basic_profiles.apply_basic_profiles(
            str(slide), str(sidecar), str(ffp), None, str(out)
        )

    assert np.all(_read(out)[1] == 500)
    assert any("no darkfield" in r.getMessage().lower() for r in caplog.records)


def test_a_profile_whose_tile_shape_disagrees_with_the_sidecar_is_refused(tmp_path):
    """A 128x128 working-size profile broadcasting against a real tile is the failure mode.

    NumPy will happily broadcast some mismatches and error confusingly on others; neither
    is an acceptable way to find out that the profiles belong to a different tile grid.
    """
    original = np.full((2, 250, 250), 1000, dtype=np.uint16)
    slide, sidecar, manifest = _tile(tmp_path, original, ["DAPI", "PANCK"])
    ffp = tmp_path / "slide_tiles-ffp.ome.tif"
    dfp = tmp_path / "slide_tiles-dfp.ome.tif"
    _write_profile(ffp, [np.ones((128, 128))])
    _write_profile(dfp, [np.zeros((128, 128))])

    with pytest.raises(ValueError, match="tile shape"):
        apply_basic_profiles.apply_basic_profiles(
            str(slide),
            str(sidecar),
            str(ffp),
            str(dfp),
            str(tmp_path / "slide_corrected.ome.tif"),
        )


# ---------------------------------------------------------------------------
# The profile stack must be usable arithmetic, not just the right shape
# ---------------------------------------------------------------------------


def test_a_zero_flatfield_is_refused_rather_than_divided_by(tmp_path):
    """Dividing by zero is silent: it yields inf, and nothing downstream notices.

    ``clip_negative_values`` -> ``detect_negative_values`` tests ``data < 0``, which is
    False for both ``inf`` and ``nan``, and ``np.round(np.clip(nan)).astype(uint16)`` is
    undefined behaviour that produces a plausible-looking integer. So a degenerate
    flatfield would reach the published slide as garbage pixels with a green run. A
    fitted flatfield is a normalised gain and is strictly positive by construction; a
    zero in it means the fit failed, not that the pixel should be amplified infinitely.
    """
    original = np.full((2, 250, 250), 1000, dtype=np.uint16)
    slide, sidecar, manifest = _tile(tmp_path, original, ["DAPI", "PANCK"])
    th, tw = manifest["tile_shape"]
    flat = np.ones((th, tw), dtype=np.float32)
    flat[0, 0] = 0.0
    ffp = tmp_path / "slide_tiles-ffp.ome.tif"
    dfp = tmp_path / "slide_tiles-dfp.ome.tif"
    _write_profile(ffp, [flat])
    _write_profile(dfp, [np.zeros((th, tw))])

    with pytest.raises(ValueError, match="strictly positive"):
        apply_basic_profiles.apply_basic_profiles(
            str(slide),
            str(sidecar),
            str(ffp),
            str(dfp),
            str(tmp_path / "slide_corrected.ome.tif"),
        )


def test_a_negative_flatfield_is_refused(tmp_path):
    """A negative gain inverts the image's sign; the clip would then zero the channel."""
    original = np.full((2, 250, 250), 1000, dtype=np.uint16)
    slide, sidecar, manifest = _tile(tmp_path, original, ["DAPI", "PANCK"])
    th, tw = manifest["tile_shape"]
    flat = np.ones((th, tw), dtype=np.float32)
    flat[5, 5] = -1.0
    ffp = tmp_path / "slide_tiles-ffp.ome.tif"
    dfp = tmp_path / "slide_tiles-dfp.ome.tif"
    _write_profile(ffp, [flat])
    _write_profile(dfp, [np.zeros((th, tw))])

    with pytest.raises(ValueError, match="strictly positive"):
        apply_basic_profiles.apply_basic_profiles(
            str(slide),
            str(sidecar),
            str(ffp),
            str(dfp),
            str(tmp_path / "slide_corrected.ome.tif"),
        )


@pytest.mark.parametrize("bad", [np.nan, np.inf])
def test_a_non_finite_darkfield_is_refused(tmp_path, bad):
    """NaN survives every downstream check: `nan < 0` is False, so the clip never sees it."""
    original = np.full((2, 250, 250), 1000, dtype=np.uint16)
    slide, sidecar, manifest = _tile(tmp_path, original, ["DAPI", "PANCK"])
    th, tw = manifest["tile_shape"]
    dark = np.zeros((th, tw), dtype=np.float32)
    dark[1, 1] = bad
    ffp = tmp_path / "slide_tiles-ffp.ome.tif"
    dfp = tmp_path / "slide_tiles-dfp.ome.tif"
    _write_profile(ffp, [np.ones((th, tw))])
    _write_profile(dfp, [dark])

    with pytest.raises(ValueError, match="not finite"):
        apply_basic_profiles.apply_basic_profiles(
            str(slide),
            str(sidecar),
            str(ffp),
            str(dfp),
            str(tmp_path / "slide_corrected.ome.tif"),
        )


def test_swapping_the_two_profiles_is_caught_at_the_shipped_defaults(tmp_path):
    """The nf-test asserts the CLI BINDING; this asserts the arithmetic notices too.

    At upstream defaults `get_darkfield` is False, so BASICPY's `*-dfp.ome.tif` is all
    zeros while `*-ffp.ome.tif` is a gain around 1.0. Handing them over the wrong way
    round therefore presents an ALL-ZERO flatfield, which the positivity check refuses.
    That is a second, semantic net under the swap the rendered-command test pins
    syntactically -- and it covers a swap introduced anywhere in the chain, including
    subworkflows/local/preprocess.nf's `.map`, which no rendered-command test can see.
    """
    original = np.full((2, 250, 250), 1000, dtype=np.uint16)
    slide, sidecar, manifest = _tile(tmp_path, original, ["DAPI", "PANCK"])
    th, tw = manifest["tile_shape"]
    real_ffp = tmp_path / "slide_tiles-ffp.ome.tif"
    real_dfp = tmp_path / "slide_tiles-dfp.ome.tif"
    _write_profile(real_ffp, [np.full((th, tw), 1.05)])
    _write_profile(real_dfp, [np.zeros((th, tw))])

    # Passed the wrong way round: darkfield where flatfield belongs and vice versa.
    with pytest.raises(ValueError, match="strictly positive"):
        apply_basic_profiles.apply_basic_profiles(
            str(slide),
            str(sidecar),
            str(real_dfp),
            str(real_ffp),
            str(tmp_path / "slide_corrected.ome.tif"),
        )
