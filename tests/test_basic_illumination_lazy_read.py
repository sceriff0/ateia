"""The two BASICPY-path readers must never eagerly decode the whole stack.

``bin/tile_for_basic.py`` and ``bin/apply_basic_profiles.py`` are the two halves of the
three-process illumination path (TILE_FOR_BASIC -> BASICPY -> APPLY_PROFILES). Both open
the slide through ``bin/utils/tiled_io.py:open_lazy`` and read it **one channel at a
time**; neither ever holds the ``(C, H, W)`` stack.

That is a MEMORY property, and memory properties rot silently: swapping
``np.asarray(arr[index, :, :])`` for ``np.asarray(arr[:])[index]`` produces identical
pixels, identical logs, identical published bytes, and a peak that scales with the marker
panel again. Every other lazy reader in this repository is pinned against exactly that
decay -- ``tests/test_split_multichannel_lazy_read.py``,
``tests/test_preprocess_qc_lazy_read.py``, ``tests/test_registration_qc_lazy_read.py``,
``tests/test_segment_lazy_dapi_read.py``. These two were the hole: the reader they
replaced (the deleted in-process ``bin/preprocess.py``) had such a test, and it was
deleted with it.

Three properties, per script:

1. **The slide is reached through ``open_lazy``**, and never through a whole-array
   ``tifffile.imread`` of the slide path. (``imread`` on the small BASICPY *profile*
   files is legitimate and is not flagged -- those are tile-sized, not slide-sized.)
2. **Every materialisation is exactly one channel.** The spy below records the size of
   every array handed back through the lazy view, so a whole-stack read is caught by its
   *bytes*, not merely by the shape of the index expression.
3. **The peak materialisation does not grow with the channel count.** The same slide at
   2 and at 6 channels must show the same largest single read. This is the O(C) -> O(1)
   claim stated as an assertion, and it is what a naive whole-stack revert fails on even
   if it were written to look per-channel.

The spy calls ``np.asarray`` on what it passes through. That is not an extra
materialisation the production code would not have done: both scripts call
``np.asarray`` on the very next line. It is the measurement instrument, and it measures
the quantity the claim is about.
"""

from __future__ import annotations

import json

import pytest

np = pytest.importorskip("numpy")
tifffile = pytest.importorskip("tifffile")
pytest.importorskip("zarr")

import apply_basic_profiles  # noqa: E402
import tile_for_basic  # noqa: E402

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _write_slide(path, stack, channel_names):
    tifffile.imwrite(
        str(path),
        stack,
        photometric="minisblack",
        metadata={"axes": "CYX", "Channel": {"Name": list(channel_names)}},
        ome=True,
    )


def _write_profile(path, planes):
    tifffile.imwrite(
        str(path), np.asarray(planes, dtype=np.float32), photometric="minisblack", ome=True
    )


def _slide(tmp_path, n_channels, h=250, w=250, seed=3):
    """A ``(C, h, w)`` uint16 slide whose channel 0 is the nuclear/fiducial marker."""
    rng = np.random.default_rng(seed)
    stack = rng.integers(0, 4000, size=(n_channels, h, w), dtype=np.uint16)
    names = ["DAPI"] + [f"MARKER_{i}" for i in range(1, n_channels)]
    path = tmp_path / "slide.ome.tif"
    _write_slide(path, stack, names)
    return path, names, stack


def _tiled(tmp_path, n_channels, fov=100):
    """Run the real TILE_FOR_BASIC step and return everything APPLY_PROFILES needs."""
    slide, names, stack = _slide(tmp_path, n_channels)
    sidecar = tmp_path / "slide_tiles.json"
    manifest = tile_for_basic.tile_for_basic(
        str(slide),
        str(tmp_path / "slide_tiles.ome.tif"),
        str(sidecar),
        channel_names=list(names),
        fov_size=(fov, fov),
        skip_nuclear=True,
        nuclear_markers=["DAPI"],
    )
    th, tw = manifest["tile_shape"]
    n_fitted = len(manifest["profile_channels"])
    ffp = tmp_path / "slide_tiles-ffp.ome.tif"
    dfp = tmp_path / "slide_tiles-dfp.ome.tif"
    _write_profile(ffp, [np.full((th, tw), 2.0)] * n_fitted)
    _write_profile(dfp, [np.zeros((th, tw))] * n_fitted)
    return slide, sidecar, ffp, dfp, names, stack, manifest


# ---------------------------------------------------------------------------
# the instrument
# ---------------------------------------------------------------------------


class _MaterialisationSpy:
    """A pass-through wrapper over a lazy ``(C, H, W)`` view that records what it hands
    back: the index expression, and the number of BYTES that expression materialised."""

    def __init__(self, arr):
        self._arr = arr
        self.shape = arr.shape
        self.dtype = arr.dtype
        self.keys = []
        self.nbytes = []

    def __getitem__(self, key):
        out = np.asarray(self._arr[key])
        self.keys.append(key)
        self.nbytes.append(int(out.nbytes))
        return out


def _install_spies(monkeypatch, module, slide_path):
    """Wrap ``module.open_lazy`` in a spy and flag any whole-array read of the slide.

    Returns ``(spies, eager_imreads)``: ``spies`` maps opened path -> spy.
    """
    spies = {}
    original_open_lazy = module.open_lazy

    def spying_open_lazy(path):
        arr, dtype, close = original_open_lazy(path)
        spy = _MaterialisationSpy(arr)
        spies[str(path)] = spy
        return spy, dtype, close

    monkeypatch.setattr(module, "open_lazy", spying_open_lazy)

    eager = []
    original_imread = tifffile.imread

    def spying_imread(path, *args, **kwargs):
        # aszarr=True is open_lazy's own call and decodes nothing. Only the SLIDE is
        # flagged: the BASICPY profile files are tile-sized and read whole by design.
        if str(path) == str(slide_path) and not kwargs.get("aszarr", False):
            eager.append(str(path))
        return original_imread(path, *args, **kwargs)

    monkeypatch.setattr(module.tifffile, "imread", spying_imread)
    return spies, eager


def _assert_single_channel_reads(spy, slide_shape, expected_reads, where):
    n_channels, h, w = slide_shape
    plane_bytes = h * w * np.dtype(np.uint16).itemsize

    assert spy.keys, f"{where}: the lazy view was opened but never read from"
    for key in spy.keys:
        assert isinstance(key, tuple) and isinstance(key[0], (int, np.integer)), (
            f"{where}: read {key!r} is not a single-channel index -- the whole stack "
            "(or a channel slab) was materialised"
        )
    assert max(spy.nbytes) == plane_bytes, (
        f"{where}: the largest single materialisation was {max(spy.nbytes)} bytes, but "
        f"one channel is {plane_bytes} bytes and the whole {n_channels}-channel stack is "
        f"{plane_bytes * n_channels} -- the read is not one channel at a time"
    )
    assert len(spy.keys) == expected_reads, (
        f"{where}: expected {expected_reads} single-channel reads, got {len(spy.keys)} "
        f"({spy.keys!r})"
    )


# ---------------------------------------------------------------------------
# TILE_FOR_BASIC
# ---------------------------------------------------------------------------


def test_tile_for_basic_never_decodes_the_whole_stack(tmp_path, monkeypatch):
    """TILE_FOR_BASIC reads one channel at a time through ``open_lazy``.

    It performs ``len(profile_channels) + 1`` reads, not ``len(profile_channels)``: the
    first fitted channel is read once up front to derive the tile grid and shape before
    the streaming write begins, and again as the write iterator's first plane. That extra
    read is one PLANE, which is the property under test -- it is asserted exactly rather
    than tolerated by an inequality, so that a change to it has to be looked at.
    """
    slide, names, stack = _slide(tmp_path, 4)
    spies, eager = _install_spies(monkeypatch, tile_for_basic, slide)

    manifest = tile_for_basic.tile_for_basic(
        str(slide),
        str(tmp_path / "tiles.ome.tif"),
        str(tmp_path / "tiles.json"),
        channel_names=list(names),
        fov_size=(100, 100),
        skip_nuclear=True,
        nuclear_markers=["DAPI"],
    )

    assert str(slide) in spies, "the slide never went through open_lazy"
    assert not eager, f"the slide was eagerly decoded in full via tifffile.imread: {eager}"
    _assert_single_channel_reads(
        spies[str(slide)],
        stack.shape,
        expected_reads=len(manifest["profile_channels"]) + 1,
        where="tile_for_basic",
    )


def test_tile_for_basic_peak_read_does_not_grow_with_channel_count(tmp_path, monkeypatch):
    """The largest single materialisation is one plane at 2 channels and at 6 -- O(1),
    not O(C). A whole-stack read passes the shape assertions of a sloppier test but
    triples here."""
    peaks = {}
    for n_channels in (2, 6):
        work = tmp_path / f"c{n_channels}"
        work.mkdir()
        slide, names, stack = _slide(work, n_channels)
        with monkeypatch.context() as ctx:
            spies, eager = _install_spies(ctx, tile_for_basic, slide)
            tile_for_basic.tile_for_basic(
                str(slide),
                str(work / "tiles.ome.tif"),
                str(work / "tiles.json"),
                channel_names=list(names),
                fov_size=(100, 100),
                skip_nuclear=True,
                nuclear_markers=["DAPI"],
            )
        assert not eager
        peaks[n_channels] = max(spies[str(slide)].nbytes)

    assert peaks[2] == peaks[6] == 250 * 250 * 2, (
        f"peak materialisation scales with the channel count: {peaks}"
    )


# ---------------------------------------------------------------------------
# APPLY_PROFILES
# ---------------------------------------------------------------------------


def test_apply_basic_profiles_never_decodes_the_whole_stack(tmp_path, monkeypatch):
    """APPLY_PROFILES reads one channel at a time through ``open_lazy`` -- including the
    nuclear/fiducial channel it does not correct, which is read (to be carried through
    untouched) but never corrected.

    Exactly one read per channel: no channel is decoded twice, and no channel is skipped.
    """
    slide, sidecar, ffp, dfp, names, stack, manifest = _tiled(tmp_path, 4)
    spies, eager = _install_spies(monkeypatch, apply_basic_profiles, slide)

    apply_basic_profiles.apply_basic_profiles(
        str(slide),
        str(sidecar),
        str(ffp),
        str(dfp),
        str(tmp_path / "slide_corrected.ome.tif"),
    )

    assert str(slide) in spies, "the slide never went through open_lazy"
    assert not eager, f"the slide was eagerly decoded in full via tifffile.imread: {eager}"
    spy = spies[str(slide)]
    _assert_single_channel_reads(
        spy, stack.shape, expected_reads=stack.shape[0], where="apply_basic_profiles"
    )
    assert [int(k[0]) for k in spy.keys] == list(range(stack.shape[0])), (
        f"expected each channel read once, in order; got {spy.keys!r}"
    )


def test_apply_basic_profiles_peak_read_does_not_grow_with_channel_count(
    tmp_path, monkeypatch
):
    """Same O(1) claim for the apply half."""
    peaks = {}
    for n_channels in (2, 6):
        work = tmp_path / f"c{n_channels}"
        work.mkdir()
        slide, sidecar, ffp, dfp, names, stack, manifest = _tiled(work, n_channels)
        with monkeypatch.context() as ctx:
            spies, eager = _install_spies(ctx, apply_basic_profiles, slide)
            apply_basic_profiles.apply_basic_profiles(
                str(slide),
                str(sidecar),
                str(ffp),
                str(dfp),
                str(work / "slide_corrected.ome.tif"),
            )
        assert not eager
        peaks[n_channels] = max(spies[str(slide)].nbytes)

    assert peaks[2] == peaks[6] == 250 * 250 * 2, (
        f"peak materialisation scales with the channel count: {peaks}"
    )


def test_the_sidecar_the_two_halves_share_is_what_pins_them_to_the_same_slide(tmp_path):
    """Not a laziness assertion -- a guard on the fixtures above.

    Both tests read the slide through the sidecar TILE_FOR_BASIC wrote. If that sidecar
    ever stopped describing the same channel set, the per-channel read counts asserted
    above would drift for a reason that has nothing to do with laziness, and the two
    tests would start disagreeing with each other silently.
    """
    slide, sidecar, ffp, dfp, names, stack, manifest = _tiled(tmp_path, 4)
    written = json.loads(sidecar.read_text())
    assert written["channel_names"] == names
    # DAPI (index 0) is the fiducial: fitted on nothing, corrected on nothing.
    assert written["profile_channels"] == [1, 2, 3]
    assert written["corrected_channels"] == [1, 2, 3]
    assert written["skipped_channels"] == [0]
