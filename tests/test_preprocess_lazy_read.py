"""``bin/preprocess.py:preprocess_multichannel_image`` must stop eagerly decoding the
whole multi-channel stack via ``tifffile.imread(image_path)`` before slicing out one
channel at a time for BaSiC correction.

Task 5 scope ruling (``.superpowers/sdd/zarr-transition/task-5-report.md``): this site
is riskier than the QC-only sites Tasks 1-4 routed through ``open_lazy`` because
channels are processed by a real ``ThreadPoolExecutor`` -- the eager code sliced
``multichannel_stack[i, ...]`` on the MAIN thread at submit time, so a naive lazy
conversion would move that slice into worker threads, concurrently, against a
SHARED tifffile/zarr store (not documented thread-safe). The fix gives each worker
its OWN ``open_lazy`` handle, opened and closed inside that worker
(``_read_and_process_channel_lazy``); see ``tests/test_preprocess_lazy_concurrency.py``
for the thread-safety proof itself. This file pins the non-concurrency properties:

1. The whole-stack eager read is gone for the common (channel-first) on-disk layout --
   the image is opened through ``open_lazy``, never a bare whole-array
   ``tifffile.imread``.
2. A channel-last (Y, X, C) layout -- never produced by this pipeline's own writers,
   but the original code's defensive branch handled it -- still falls back to the
   OLD eager whole-stack read + transpose, byte-identical to the pre-change code.
3. ``log_image_stats``'s global "input" statistics line (min/max/mean, high-value
   warning) is preserved via a streaming aggregator, not silently dropped or
   fragmented into one line per channel.
4. The final saved OME-TIFF is pixel-identical to the OLD whole-stack-read code path,
   for both the channel-first (lazy) and channel-last (eager fallback) layouts.
"""

from __future__ import annotations

import logging
import sys
import types

# basicpy isn't installed in every local dev environment, but bin/preprocess.py
# imports it eagerly at module scope. This test only exercises the read path and
# the (stubbed) BaSiC correction call, so stub just enough of the module surface to
# import it -- same technique as tests/test_preprocess_channel_skip.py. In
# CI/containers with the real basicpy installed, this stub is never used.
try:
    import basicpy  # noqa: F401
except ImportError:
    stub = types.ModuleType("basicpy")

    class _StubBaSiC:
        def __init__(self, *args, **kwargs):
            pass

    stub.BaSiC = _StubBaSiC
    stub.__version__ = "0.0.0-stub"
    sys.modules["basicpy"] = stub

import numpy as np  # noqa: E402
import pytest  # noqa: E402

tifffile = pytest.importorskip("tifffile")
pytest.importorskip("zarr")

from bin import preprocess  # noqa: E402


def _write_stack(tmp_path, n_channels, h=48, w=40, seed=7, axes="CYX", dtype=np.uint16):
    """A small multichannel OME-TIFF, planar (C, H, W) by default.

    Each channel gets visibly distinct pixel content (a per-channel additive offset
    on top of per-channel random noise) so that any cross-channel mixup under
    concurrency -- the failure mode Task 5's ruling calls out as "silently wrong
    pixels, not a crash" -- is detectable by direct comparison, not just a shape/dtype
    check.
    """
    rng = np.random.default_rng(seed)
    planes = []
    for c in range(n_channels):
        base = rng.integers(0, 5000, size=(h, w), dtype=np.uint32)
        plane = (base + c * 10_000).astype(dtype)
        planes.append(plane)
    stack = np.stack(planes, axis=0)  # (C, H, W)

    path = tmp_path / "image.ome.tiff"
    if axes == "YXC":
        stack = np.transpose(stack, (1, 2, 0))  # (H, W, C)
        tifffile.imwrite(str(path), stack, photometric="minisblack")
    else:
        tifffile.imwrite(
            str(path),
            stack,
            ome=True,
            photometric="minisblack",
            metadata={"axes": axes},
        )
    return path, stack


def _write_stack_from_array(tmp_path, stack):
    """Write an explicit (C, H, W) array as a channel-first OME-TIFF, same encoding
    ``_write_stack`` uses for its default axes="CYX" case -- but with full control over the
    pixel values, for fixtures that need to land specific values (negatives, wrapped-negative
    highs) rather than ``_write_stack``'s generic per-channel-offset noise.
    """
    path = tmp_path / "image.ome.tiff"
    tifffile.imwrite(
        str(path),
        stack,
        ome=True,
        photometric="minisblack",
        metadata={"axes": "CYX"},
    )
    return path


def _channel_names(n):
    return [f"CH{i}" for i in range(n)]


def _identity_basic(monkeypatch):
    """Stub apply_basic_correction as a pure identity so output pixels equal input
    pixels exactly -- isolates the READ path from BaSiC's own (real, iterative,
    untested-here) numerics.
    """
    monkeypatch.setattr(
        preprocess,
        "apply_basic_correction",
        lambda image, fov_size=None: (image.copy(), object()),
    )


def _old_preprocess_multichannel_image_stats_and_pixels(image_path, channel_names):
    """Reimplements the PRE-TASK-5 whole-stack read: eager ``tifffile.imread``, the
    ndim==2 / channel-last transpose branch, then a plain per-channel slice. Used
    as an independent "old" reference for the equivalence tests below -- mirrors
    the reference-implementation pattern in
    ``tests/test_preprocess_qc_lazy_read.py::_old_generate_preprocess_qc``.
    """
    stack = tifffile.imread(image_path)
    if stack.ndim == 2:
        stack = np.expand_dims(stack, axis=0)
    elif (
        stack.ndim == 3
        and stack.shape[2] == len(channel_names)
        and stack.shape[0] != len(channel_names)
    ):
        stack = np.transpose(stack, (2, 0, 1))
    return stack


def test_lazy_read_never_eagerly_decodes_whole_stack_for_channel_first_layout(
    tmp_path, monkeypatch
):
    """Fails against the pre-change code: it has no ``open_lazy`` attribute at all on
    the ``preprocess`` module, and always calls a bare whole-array
    ``tifffile.imread(image_path)``.
    """
    n_channels = 4
    image_path, _stack = _write_stack(tmp_path, n_channels)
    channel_names = _channel_names(n_channels)
    _identity_basic(monkeypatch)

    seen_lazy_paths = []
    orig_open_lazy = preprocess.open_lazy

    def spying_open_lazy(path):
        seen_lazy_paths.append(str(path))
        return orig_open_lazy(path)

    monkeypatch.setattr(preprocess, "open_lazy", spying_open_lazy)

    whole_array_imread_calls = []
    orig_imread = tifffile.imread

    def spying_imread(path, *args, **kwargs):
        if not kwargs.get("aszarr", False):
            whole_array_imread_calls.append(str(path))
        return orig_imread(path, *args, **kwargs)

    monkeypatch.setattr(preprocess.tifffile, "imread", spying_imread)

    out_path = tmp_path / "out.ome.tiff"
    preprocess.preprocess_multichannel_image(
        image_path=str(image_path),
        channel_names=list(channel_names),
        output_path=str(out_path),
        skip_nuclear=False,
        n_workers=2,
    )

    assert len(seen_lazy_paths) == n_channels, (
        f"expected one open_lazy call per channel (each worker opens its own "
        f"handle), got {len(seen_lazy_paths)}: {seen_lazy_paths}"
    )
    assert all(p == str(image_path) for p in seen_lazy_paths)
    assert str(image_path) not in whole_array_imread_calls, (
        "image was still eagerly decoded in full via tifffile.imread"
    )


def test_lazy_read_output_matches_old_whole_stack_read(tmp_path, monkeypatch):
    """The saved OME-TIFF must be pixel-identical to the pre-change (eager) code path."""
    n_channels = 5
    image_path, _stack = _write_stack(tmp_path, n_channels)
    channel_names = _channel_names(n_channels)

    expected_stack = _old_preprocess_multichannel_image_stats_and_pixels(
        image_path, channel_names
    )

    _identity_basic(monkeypatch)
    out_path = tmp_path / "out.ome.tiff"
    preprocess.preprocess_multichannel_image(
        image_path=str(image_path),
        channel_names=list(channel_names),
        output_path=str(out_path),
        skip_nuclear=False,
        n_workers=3,
    )

    got_stack = tifffile.imread(str(out_path))
    assert got_stack.shape == expected_stack.shape
    assert got_stack.dtype == expected_stack.dtype
    # apply_basic_correction is identity here, so output must equal the RAW input.
    assert np.array_equal(got_stack, expected_stack)


def test_channel_last_layout_falls_back_to_eager_read_and_matches_old_code(
    tmp_path, monkeypatch
):
    """A hypothetical (Y, X, C) input -- never produced by this pipeline's own
    writers, but the original defensive branch handled it -- must still fall back
    to a whole-stack read and produce output identical to the pre-change code.
    """
    n_channels = 3
    image_path, _stack = _write_stack(tmp_path, n_channels, axes="YXC")
    channel_names = _channel_names(n_channels)

    expected_stack = _old_preprocess_multichannel_image_stats_and_pixels(
        image_path, channel_names
    )
    assert expected_stack.shape[0] == n_channels  # sanity: transpose branch fired

    _identity_basic(monkeypatch)

    # The channel-last fallback must NOT touch open_lazy at all -- it's the exact old
    # eager path.
    lazy_calls = []
    orig_open_lazy = preprocess.open_lazy
    monkeypatch.setattr(
        preprocess,
        "open_lazy",
        lambda path: (lazy_calls.append(str(path)), orig_open_lazy(path))[1],
    )

    out_path = tmp_path / "out.ome.tiff"
    preprocess.preprocess_multichannel_image(
        image_path=str(image_path),
        channel_names=list(channel_names),
        output_path=str(out_path),
        skip_nuclear=False,
        n_workers=2,
    )

    assert lazy_calls == [], f"channel-last layout must not use open_lazy, saw {lazy_calls}"

    got_stack = tifffile.imread(str(out_path))
    assert got_stack.shape == expected_stack.shape
    assert np.array_equal(got_stack, expected_stack)


def test_streaming_input_stats_log_matches_whole_array_log_image_stats(
    tmp_path, monkeypatch, caplog
):
    """The streamed "[input] Image stats: ..." line must report the SAME min/max/mean
    a single whole-array ``log_image_stats`` call would have logged -- not one line
    per channel, and not silently dropped.
    """
    n_channels = 4
    image_path, stack = _write_stack(tmp_path, n_channels)
    channel_names = _channel_names(n_channels)
    _identity_basic(monkeypatch)

    # Compute the expected line using the untouched whole-array log_image_stats,
    # against the RAW on-disk stack (matches what the pre-change code passed it,
    # before any dtype casting downstream).
    ref_logger = logging.getLogger("preprocess_lazy_read_reference")
    ref_logger.setLevel(logging.INFO)
    ref_records = []

    class _Collect(logging.Handler):
        def emit(self, record):
            ref_records.append(record.getMessage())

    ref_logger.addHandler(_Collect())
    from bin.utils.validation import log_image_stats

    log_image_stats(stack, "input", ref_logger)
    expected_lines = list(ref_records)
    assert expected_lines, "reference log_image_stats produced no output"

    out_path = tmp_path / "out.ome.tiff"
    with caplog.at_level(logging.INFO, logger=preprocess.logger.name):
        preprocess.preprocess_multichannel_image(
            image_path=str(image_path),
            channel_names=list(channel_names),
            output_path=str(out_path),
            skip_nuclear=False,
            n_workers=2,
        )

    got_lines = [
        r.message
        for r in caplog.records
        if r.name == preprocess.logger.name and "[input] Image stats:" in r.message
    ]
    assert len(got_lines) == 1, f"expected exactly one aggregate stats line, got {got_lines}"
    assert got_lines[0] == expected_lines[0]


def _captured_input_lines(tmp_path, image_path, channel_names, monkeypatch, caplog, n_workers=2):
    """Run the lazy preprocess path and return every ``[input] ...`` line
    (``_StreamingImageStats.finalize``) it logged, in emission order -- the stats line and any
    of its two warning branches (negative-values / suspicious-high-uint16).
    """
    out_path = tmp_path / "out.ome.tiff"
    with caplog.at_level(logging.INFO, logger=preprocess.logger.name):
        preprocess.preprocess_multichannel_image(
            image_path=str(image_path),
            channel_names=list(channel_names),
            output_path=str(out_path),
            skip_nuclear=False,
            n_workers=n_workers,
        )
    return [
        r.message
        for r in caplog.records
        if r.name == preprocess.logger.name and r.message.startswith("[input]")
    ]


def test_streaming_input_stats_negative_value_warning_matches_whole_array_log_image_stats(
    tmp_path, monkeypatch, caplog
):
    """``_StreamingImageStats.finalize``'s ``if self._min < 0`` branch (preprocess.py:303-306)
    must fire, and must log the SAME "Negative values detected" line a whole-array
    ``log_image_stats`` call would produce -- not just the plain "Image stats:" line.

    Mutating ``self._min < 0`` to ``self._min < -1`` (or stubbing the branch out) turns this RED:
    the streamed aggregate would silently drop the negative-values warning while
    ``log_image_stats`` (the reference) still emits it, so ``got_lines != expected_lines``.
    """
    n_channels = 3
    h, w = 20, 24
    rng = np.random.default_rng(11)
    stack = np.stack(
        [rng.integers(-1000, 1000, size=(h, w)).astype(np.int16) for _ in range(n_channels)],
        axis=0,
    )
    assert stack.min() < 0, "fixture must actually exercise the negative-values branch"
    image_path = _write_stack_from_array(tmp_path, stack)
    channel_names = _channel_names(n_channels)
    _identity_basic(monkeypatch)

    ref_logger = logging.getLogger("preprocess_lazy_read_reference_negative")
    ref_logger.setLevel(logging.INFO)
    ref_records = []

    class _Collect(logging.Handler):
        def emit(self, record):
            ref_records.append(record.getMessage())

    ref_logger.addHandler(_Collect())
    from bin.utils.validation import log_image_stats

    log_image_stats(stack, "input", ref_logger)
    expected_lines = list(ref_records)
    assert len(expected_lines) == 2, (
        f"reference log_image_stats should emit exactly the stats line plus the negative-values "
        f"warning, got {expected_lines}"
    )
    assert "Negative values detected" in expected_lines[1]

    got_lines = _captured_input_lines(tmp_path, image_path, channel_names, monkeypatch, caplog)
    assert got_lines == expected_lines


def test_streaming_input_stats_high_uint16_warning_matches_whole_array_log_image_stats(
    tmp_path, monkeypatch, caplog
):
    """``_StreamingImageStats.finalize``'s ``if self._dtype == np.uint16 and self._max > 60000``
    branch (preprocess.py:308-314) must fire, and must log the SAME "Suspicious high values
    (potential wrapped negatives)" line -- with the SAME aggregate pixel count and percentage --
    a whole-array ``log_image_stats`` call would produce.

    Mutating ``self._high_count += c_high`` to ``+= 0`` (as the reviewer's mutation did) turns
    this RED: the streamed aggregate's high-pixel count collapses to 0, the percentage drops
    below the 0.1% gate, and the warning line is silently dropped while the reference
    ``log_image_stats`` (computed from the whole array) still emits it.
    """
    n_channels = 4
    h, w = 40, 40
    rng = np.random.default_rng(13)
    stack = np.stack(
        [rng.integers(0, 5000, size=(h, w)).astype(np.uint16) for _ in range(n_channels)],
        axis=0,
    )
    # 20 / 6400 pixels = 0.3125% > the 0.1% warning gate, split across two channels so no
    # single channel's own count alone would clear the gate -- exercising the AGGREGATE
    # (cross-channel-summed) high-count, not a per-channel one.
    stack[0, 0, :10] = 65000
    stack[1, 0, :10] = 65000
    high_frac = (stack > 60000).sum() / stack.size * 100
    assert high_frac > 0.1, "fixture must actually clear the suspicious-high-values gate"

    image_path = _write_stack_from_array(tmp_path, stack)
    channel_names = _channel_names(n_channels)
    _identity_basic(monkeypatch)

    ref_logger = logging.getLogger("preprocess_lazy_read_reference_high_uint16")
    ref_logger.setLevel(logging.INFO)
    ref_records = []

    class _Collect(logging.Handler):
        def emit(self, record):
            ref_records.append(record.getMessage())

    ref_logger.addHandler(_Collect())
    from bin.utils.validation import log_image_stats

    log_image_stats(stack, "input", ref_logger)
    expected_lines = list(ref_records)
    assert len(expected_lines) == 2, (
        f"reference log_image_stats should emit exactly the stats line plus the suspicious-high "
        f"warning, got {expected_lines}"
    )
    assert "Suspicious high values" in expected_lines[1]

    got_lines = _captured_input_lines(tmp_path, image_path, channel_names, monkeypatch, caplog)
    assert got_lines == expected_lines
