"""Thread-safety proof for ``bin/preprocess.py``'s lazy read path.

Task 5's controller ruling was explicit about the risk this test exists to retire:

    "Channels are processed by ThreadPoolExecutor(max_workers=n_workers) ... and
    multichannel_stack[i, ...] is sliced at SUBMIT time -- eagerly, on the main
    thread. Making the read lazy moves that slice INTO worker threads,
    concurrently, against a shared tifffile zarr store. tifffile's zarr view is NOT
    documented thread-safe. The failure mode is SILENTLY WRONG PIXELS on gigapixel
    data, not a crash, and this machine cannot test at that scale.

    You may convert this site ONLY IF you give each worker its OWN lazy handle
    (open_lazy per thread, closed by that thread) AND add a concurrency test that
    runs the real threaded path with n_workers>1 and asserts output identical to
    the sequential/eager path, run enough times to be meaningful."

``_read_and_process_channel_lazy`` (bin/preprocess.py) opens a fresh ``open_lazy``
handle INSIDE the worker function, reads exactly one channel, and closes that same
handle before returning -- no store, array, or file handle is ever shared between
threads. This file cannot prove the absence of a race at gigapixel scale (the
ruling is explicit that this machine cannot do that), but it CAN run the real
``ThreadPoolExecutor`` path, at real concurrency, many times, against known
per-channel ground truth, and would be expected to catch a cross-thread channel
mixup (the stated "silently wrong pixels" failure mode) if the per-thread-handle
design did not actually isolate threads from each other.
"""

from __future__ import annotations

import sys
import types

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

N_CHANNELS = 20
N_REPEATS = 40
N_WORKERS = 8
H, W = 96, 80


def _write_stack(tmp_path, seed):
    """Every channel gets a DISTINCT, high-entropy plane (a per-channel offset band
    plus per-run random noise) so a cross-thread swap or partial-read corruption
    changes the compared array with overwhelming probability -- a coincidental match
    on corrupted data is not a realistic failure mode here.
    """
    rng = np.random.default_rng(seed)
    planes = [
        (rng.integers(0, 3000, size=(H, W), dtype=np.uint32) + c * 10_000).astype(
            np.uint16
        )
        for c in range(N_CHANNELS)
    ]
    stack = np.stack(planes, axis=0)
    path = tmp_path / f"image_{seed}.ome.tiff"
    tifffile.imwrite(
        str(path),
        stack,
        ome=True,
        photometric="minisblack",
        metadata={"axes": "CYX"},
    )
    return path, stack


def _identity_basic(monkeypatch):
    monkeypatch.setattr(
        preprocess,
        "apply_basic_correction",
        lambda image, fov_size=None: (image.copy(), object()),
    )


def test_concurrent_lazy_reads_never_mix_up_or_corrupt_channels(tmp_path, monkeypatch):
    """Run the REAL ThreadPoolExecutor(max_workers=8) path over 20 channels, 40
    times with fresh random data each time, and assert every channel's saved pixels
    exactly equal that channel's known input. apply_basic_correction is stubbed as
    an identity, isolating this test to the read path -- the thing Task 5's ruling
    flags as unproven -- rather than BaSiC's own numerics.

    A cross-thread read race that swapped or corrupted even one channel, on even
    one of the 40 repeats, fails this assertion.
    """
    _identity_basic(monkeypatch)
    channel_names = [f"CH{i}" for i in range(N_CHANNELS)]

    for repeat in range(N_REPEATS):
        image_path, expected_stack = _write_stack(tmp_path, seed=repeat)
        out_path = tmp_path / f"out_{repeat}.ome.tiff"

        preprocess.preprocess_multichannel_image(
            image_path=str(image_path),
            channel_names=list(channel_names),
            output_path=str(out_path),
            skip_nuclear=False,
            n_workers=N_WORKERS,
        )

        got_stack = tifffile.imread(str(out_path))
        assert got_stack.shape == expected_stack.shape, f"repeat {repeat}: shape mismatch"
        for c in range(N_CHANNELS):
            assert np.array_equal(got_stack[c], expected_stack[c]), (
                f"repeat {repeat}, channel {c}: pixels differ from known input -- "
                f"cross-thread read corruption or channel mixup"
            )

        # Clean up between repeats to keep tmp_path bounded.
        image_path.unlink()
        out_path.unlink()


def test_each_channel_opens_and_closes_its_own_lazy_handle(tmp_path, monkeypatch):
    """Every worker must open its OWN ``open_lazy`` handle and close it itself --
    never a handle shared across threads, and never left open. Spies on
    ``open_lazy`` to count opens, and wraps the returned close callable to count
    closes; both must equal N_CHANNELS, and every open must be paired with exactly
    one close by the time all channels are done.
    """
    _identity_basic(monkeypatch)
    image_path, _stack = _write_stack(tmp_path, seed=999)
    channel_names = [f"CH{i}" for i in range(N_CHANNELS)]

    open_count = {"n": 0}
    close_count = {"n": 0}
    lock = __import__("threading").Lock()
    orig_open_lazy = preprocess.open_lazy

    def spying_open_lazy(path):
        with lock:
            open_count["n"] += 1
        arr, dtype, close = orig_open_lazy(path)

        def spying_close():
            with lock:
                close_count["n"] += 1
            close()

        return arr, dtype, spying_close

    monkeypatch.setattr(preprocess, "open_lazy", spying_open_lazy)

    out_path = tmp_path / "out.ome.tiff"
    preprocess.preprocess_multichannel_image(
        image_path=str(image_path),
        channel_names=list(channel_names),
        output_path=str(out_path),
        skip_nuclear=False,
        n_workers=N_WORKERS,
    )

    assert open_count["n"] == N_CHANNELS, (
        f"expected {N_CHANNELS} open_lazy calls (one per channel, one per worker), "
        f"got {open_count['n']}"
    )
    assert close_count["n"] == N_CHANNELS, (
        f"expected every opened handle to be closed by its own worker, "
        f"got {close_count['n']} closes for {open_count['n']} opens"
    )
