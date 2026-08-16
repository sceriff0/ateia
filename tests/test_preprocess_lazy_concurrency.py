"""Thread-safety evidence for ``bin/preprocess.py``'s lazy read path.

Task 5's controller ruling was explicit about the risk this file exists to retire:

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
threads.

FIX ROUND 1 -- what this file's first version got wrong, and what changed
---------------------------------------------------------------------------
The first version of this file ran the real ThreadPoolExecutor path 40 times and
found zero mismatches, and its docstring claimed this "would be expected to catch a
cross-thread channel mixup". A review ran the SAME workload with ``open_lazy``
monkeypatched to hand ONE SHARED store to all 8 threads (``close`` a no-op) and
found: 0/40 repeats detected corruption. The harness was not blind to every hazard
(a deliberate off-by-one channel-index swap IS caught -- see
``test_concurrent_lazy_reads_never_mix_up_or_corrupt_channels``), but it was blind
to the SPECIFIC shared-store hazard this file exists to retire, and the original
claim overstated what had been shown. Investigating why turned up the actual
mechanism (recorded here because it changes what "proof" has to mean for this
site):

- ``tifffile.tifffile.FileHandle.seek()`` and ``.read()`` are two separate,
  non-atomic Python calls against one shared, mutable file position -- confirmed by
  reading the source, not assumed. This IS a real race window in principle.
- BUT ``tifffile.tifffile.ZarrTiffStore.__init__`` -- the class backing
  ``tifffile.imread(path, aszarr=True)``, which ``open_lazy`` calls -- enables its
  OWN internal ``threading.RLock`` by default (``if lock is None: fh.set_lock(True)``)
  and serializes reads through it. This repo's pinned tifffile (2025.5.10 in CI,
  2023.4.12 locally) therefore already protects even a genuinely SHARED handle's
  concurrent reads, on its own, for reasons neither ``tiled_io.py`` nor this
  pipeline has ever asked for or documented -- which is exactly why 40 repeats
  (or, tried afterward: real threads, real 4000x4000 planes, an explicit
  post-seek ``time.sleep`` to widen the window) could not produce a single
  corrupted read even for a deliberately shared handle. The review's "0/40" result
  was not a fluke; it is what this specific tifffile version's own internal locking
  produces, independent of whether ``bin/preprocess.py``'s design is correct.

Because that protection is an undocumented implementation detail of the CURRENTLY
PINNED tifffile version -- not a contract ``tiled_io.py`` or this pipeline relies
on, and not guaranteed to survive a version bump -- the negative control below
deliberately DEFEATS it (reaches into the zarr store and replaces its internal
``RLock`` with a no-op) before comparing the shared-handle design against the
per-thread-handle one. With that confound removed:
- shared handle, lock defeated, race window widened -> reliably corrupts or raises
  (``TiffFileError('corrupted strip ...')``), see
  ``test_shared_handle_negative_control_is_actually_caught``.
- per-thread handles, SAME lock-defeat and window-widening applied to EACH
  thread's own store -> no corruption, see
  ``test_per_thread_handles_survive_the_identical_fault_injection``. Per-thread
  handles share no mutable state with each other, so defeating one thread's own
  (otherwise-unused) lock has no effect on any other thread.

What this file establishes, and what it does not
--------------------------------------------------
1. The negative-control pair above IS discriminating: it goes RED for the forbidden
   shared-handle design and GREEN for this file's actual per-thread-handle design,
   under an identical fault injection applied symmetrically to both. That is the
   actual thread-safety evidence for the current implementation.
2. ``test_concurrent_lazy_reads_never_mix_up_or_corrupt_channels`` (40 repeats, no
   fault injection) remains in this file as an ordinary regression/mixup guard --
   it DOES catch a deliberate off-by-one channel-index bug -- but it does NOT, by
   itself, discriminate the per-thread-handle design from the forbidden
   shared-handle one, because (per above) this tifffile version happens to protect
   both. Do not read a pass of that test alone as thread-safety evidence.
3. Nothing here proves the absence of a race at gigapixel scale, on a different
   filesystem, or under a different tifffile version's locking defaults -- this
   machine cannot exercise that, and the design argument (no state is EVER shared
   between per-thread handles, so there is nothing to race on regardless of any
   particular tifffile version's internal locking) is what this design leans on
   beyond what was directly tested here.

FIX ROUND 2 -- the negative control was SELF-CERTIFYING, now fixed
---------------------------------------------------------------------------
Fix round 1's ``test_shared_handle_negative_control_is_actually_caught`` caught its
corruption/error signal with a bare ``except Exception: corrupted = True``. That is
a defect of exactly the same shape this whole round exists to remove:
``_defeat_tifffile_internal_lock`` walks a PRIVATE tifffile attribute chain
(``arr._z.chunk_store._mutable_mapping._filecache.lock``); if any link in that
chain ever disappears in a future tifffile version, walking it raises
``AttributeError`` inside a worker thread, that surfaces via ``future.result()``,
and the bare ``except Exception`` would have swallowed it and reported
"corruption detected" -- PASSING while the negative control had actually proven
nothing at all. Fixed by:
1. ``_defeat_tifffile_internal_lock`` now self-verifies its own replacement took
   effect (raises plainly, not silently, if the chain or the check fails) --
   deliberately using a distinct exception, never one the corruption-catch below
   would treat as "hazard detected".
2. The negative control's except clause is narrowed from bare ``Exception`` to
   ``tifffile.TiffFileError`` ONLY -- the single exception type actually observed
   across every empirical corruption trial (``TiffFileError('corrupted strip
   cannot be reshaped from ... to ...')``). Any other exception -- an
   ``AttributeError``/``_LockDefeatFailed`` from a broken lock-defeat chain, a
   ``threading.BrokenBarrierError`` from a stalled barrier, a plain bug -- now
   propagates OUT of the test uncaught, failing it visibly instead of reading as
   "corruption detected".
``test_negative_control_is_not_self_certifying_when_lock_defeat_breaks`` proves
this directly: it deliberately breaks ``_defeat_tifffile_internal_lock`` (simulating
a future tifffile restructuring) and asserts the resulting ``AttributeError``
propagates all the way out of ``preprocess_multichannel_image`` rather than being
absorbed. See task-5-report.md's "Fix round 2" section for the verbatim RED/GREEN
transcript proving the OLD (bare-except) code would have passed silently here,
and the NEW (narrowed) code fails loudly.

Also recorded (not fixable here): the negative control's discriminating power
depends entirely on ``_filecache.lock`` remaining tifffile's SOLE synchronization
point for concurrent ``ZarrTiffStore`` reads. A future tifffile version that adds a
SECOND, different synchronization mechanism alongside or instead of
``_filecache.lock`` would leave this control green-by-luck (defeating the wrong /
an incomplete lock), not red. This control demonstrates the shared-vs-per-thread
DESIGN difference under today's tifffile internals; it is not a permanent
guarantee against every future tifffile locking strategy.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
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

# Parameters for the fault-injected negative control. n_channels == n_workers so
# every channel is dispatched to ThreadPoolExecutor concurrently (one task per
# worker, no queueing) -- required for the Barrier below, which needs every party
# to actually arrive. 300x300 was tuned empirically (see task-5-report.md's
# fix-round-1 section): large enough that a real read is not instantaneous, small
# enough that the whole test suite stays fast.
FAULT_N = 8
FAULT_H, FAULT_W = 300, 300
_SEEK_HOLD_SECONDS = 0.01


def _write_stack(tmp_path, seed, n_channels=N_CHANNELS, h=H, w=W):
    """Every channel gets a DISTINCT, high-entropy plane (a per-channel offset band
    plus per-run random noise) so a cross-thread swap or partial-read corruption
    changes the compared array with overwhelming probability -- a coincidental match
    on corrupted data is not a realistic failure mode here.
    """
    rng = np.random.default_rng(seed)
    planes = [
        (rng.integers(0, 3000, size=(h, w), dtype=np.uint32) + c * 10_000).astype(
            np.uint16
        )
        for c in range(n_channels)
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
    """Ordinary regression/mixup guard, NOT thread-safety evidence on its own (see
    module docstring, "FIX ROUND 1"). Runs the REAL ThreadPoolExecutor(max_workers=8)
    path over 20 channels, 40 times with fresh random data each time, and asserts
    every channel's saved pixels exactly equal that channel's known input.
    apply_basic_correction is stubbed as an identity, isolating this test to the
    read path rather than BaSiC's own numerics.

    A cross-thread channel-index mixup, on even one of the 40 repeats, fails this
    assertion -- confirmed by mutation: temporarily swapping
    ``_read_and_process_channel_lazy``'s channel index by one position makes this
    test fail immediately. What this test does NOT do is discriminate the
    per-thread-handle design from a shared-handle one: this pipeline's pinned
    tifffile version happens to serialize even a shared handle's reads via its own
    internal lock, so this exact assertion would pass identically under the
    forbidden design too. See ``test_shared_handle_negative_control_is_actually_caught``
    / ``test_per_thread_handles_survive_the_identical_fault_injection`` for the test
    that actually discriminates the two designs.
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

    Also asserts THREAD IDENTITY per handle: ``threading.get_ident()`` recorded at
    open time must equal the ident recorded when THAT SAME handle's close is
    called. If a handle were ever handed to, or closed by, a different thread than
    the one that opened it, this fails even though the open/close COUNTS above
    would still balance.
    """
    _identity_basic(monkeypatch)
    image_path, _stack = _write_stack(tmp_path, seed=999)
    channel_names = [f"CH{i}" for i in range(N_CHANNELS)]

    open_count = {"n": 0}
    close_count = {"n": 0}
    idents = []  # list of (open_ident, close_ident) per handle
    lock = threading.Lock()
    orig_open_lazy = preprocess.open_lazy

    def spying_open_lazy(path):
        open_ident = threading.get_ident()
        with lock:
            open_count["n"] += 1
        arr, dtype, close = orig_open_lazy(path)

        def spying_close():
            close_ident = threading.get_ident()
            with lock:
                close_count["n"] += 1
                idents.append((open_ident, close_ident))
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
    assert len(idents) == N_CHANNELS
    for open_ident, close_ident in idents:
        assert open_ident == close_ident, (
            f"handle opened on thread {open_ident} but closed on thread "
            f"{close_ident} -- a handle crossed threads"
        )


# ---------------------------------------------------------------------------
# Fault-injected negative control (fix round 1; hardened in fix round 2 -- see the
# module docstring's "FIX ROUND 1" / "FIX ROUND 2" sections).
# ---------------------------------------------------------------------------


class _LockDefeatFailed(Exception):
    """Raised by ``_defeat_tifffile_internal_lock`` when it cannot locate or
    verify tifffile's internal lock.

    Deliberately a DISTINCT exception type, and deliberately NOT one of the
    exception types ``test_shared_handle_negative_control_is_actually_caught``
    treats as "hazard detected" (that test catches only ``tifffile.TiffFileError``,
    see fix round 2). If this fires, the negative-control HARNESS is broken --
    e.g. a future tifffile version restructured the private attribute chain this
    helper walks -- and that must surface as a genuine test failure, never be
    misread as "corruption detected, control PASSED".
    """


def _defeat_tifffile_internal_lock(arr):
    """Reach into a zarr Array opened via ``open_lazy`` and replace its underlying
    ``tifffile.tifffile.ZarrTiffStore``'s internal ``RLock`` with a no-op.

    ``ZarrTiffStore.__init__`` enables this lock by default
    (``if lock is None: fh.set_lock(True)``) and every store read goes through it,
    which is why a genuinely SHARED handle does not corrupt on its own with this
    tifffile version (see module docstring). That protection is an undocumented
    implementation detail this pipeline has never asked for; defeating it here
    removes it from the experiment so the comparison below is actually about the
    shared-vs-per-thread-handle DESIGN, not about whether the current tifffile
    version happens to also protect a design this pipeline does not intend to rely
    on.

    Self-verifies the replacement actually took effect (fix round 2): if the
    private attribute chain doesn't exist, or the lock is somehow still the
    original object afterward, raises ``_LockDefeatFailed`` -- never silently
    proceeds as if the defeat had worked when it had not.
    """
    try:
        mm = arr._z.chunk_store._mutable_mapping
        original_lock = mm._filecache.lock
        mm._filecache.lock = contextlib.nullcontext()
        new_lock = mm._filecache.lock
    except AttributeError as exc:
        raise _LockDefeatFailed(
            f"could not locate ZarrTiffStore's internal lock via "
            f"arr._z.chunk_store._mutable_mapping._filecache.lock: {exc!r} -- "
            f"tifffile's private structure may have changed"
        ) from exc

    # Identity check (not the weaker isinstance-of-context-manager check --
    # threading.RLock ALSO satisfies contextlib.AbstractContextManager via duck
    # typing, so that alone would not detect "still the original lock").
    if new_lock is original_lock or not isinstance(new_lock, type(contextlib.nullcontext())):
        raise _LockDefeatFailed(
            f"lock replacement did not take effect -- _filecache.lock is still "
            f"{new_lock!r}"
        )


def _hold_after_seek(monkeypatch):
    """Widen the seek-then-read race window for the duration of the test.

    ``tifffile.tifffile.FileHandle.seek()`` and ``.read()`` are two separate,
    non-atomic calls against one shared, mutable file position (confirmed by
    reading ``FileHandle``'s source: ``seek`` calls ``self._fh.seek(...)``, ``read``
    calls ``self._fh.read(...)``, with no synchronization of its own inside
    ``FileHandle`` itself -- only ``ZarrTiffStore``'s own RLock, defeated above,
    wraps calls through it). Sleeping after every real seek releases the GIL,
    giving any OTHER thread racing the SAME underlying file object a real chance to
    seek elsewhere before this thread reads.
    """
    real_seek = tifffile.tifffile.FileHandle.seek

    def held_seek(self, offset, whence=0):
        result = real_seek(self, offset, whence)
        time.sleep(_SEEK_HOLD_SECONDS)
        return result

    monkeypatch.setattr(tifffile.tifffile.FileHandle, "seek", held_seek)


def _make_synced_open_lazy(real_open_lazy, barrier, share_handle):
    """Build an ``open_lazy`` replacement for the negative control.

    Every store it returns has its internal tifffile lock defeated
    (``_defeat_tifffile_internal_lock``). Every array it returns wraps the raw
    per-channel read in a ``barrier.wait()`` immediately before the read executes,
    so every worker's actual pixel read is forced to start at (as close as
    CPython's GIL/scheduler allows) the same instant -- removing luck-of-the-draw
    OS-scheduling timing from whether the race window is hit.

    If ``share_handle`` is True, every caller gets the SAME underlying array and a
    no-op ``close`` -- the FORBIDDEN "one handle shared across every worker thread"
    design this whole file exists to rule out. If False, every caller opens its own
    handle -- the real ``_read_and_process_channel_lazy`` design -- with the same
    lock-defeat and barrier applied per-handle; since nothing is shared between
    separate handles, this has no effect on correctness for that design.
    """
    state = {}
    open_state_lock = threading.Lock()

    class _BarrieredArr:
        def __init__(self, real_arr):
            self._real = real_arr
            self.shape = real_arr.shape
            self.dtype = real_arr.dtype

        def __getitem__(self, key):
            barrier.wait(timeout=30)
            return self._real[key]

    def synced_open_lazy(path):
        if share_handle:
            with open_state_lock:
                if "h" not in state:
                    arr, dtype, _close = real_open_lazy(path)
                    _defeat_tifffile_internal_lock(arr)
                    state["h"] = (arr, dtype)
            arr, dtype = state["h"]
            close = lambda: None  # noqa: E731 -- never releases the shared store
        else:
            arr, dtype, close = real_open_lazy(path)
            _defeat_tifffile_internal_lock(arr)

        return _BarrieredArr(arr), dtype, close

    return synced_open_lazy


def test_shared_handle_negative_control_is_actually_caught(tmp_path, monkeypatch):
    """THE control this file's other assertions must not be blind to.

    Monkeypatches ``preprocess.open_lazy`` to the FORBIDDEN design: one handle,
    opened once, shared by all 8 workers, ``close`` a no-op. Combined with
    ``_hold_after_seek`` and the internal-lock defeat inside
    ``_make_synced_open_lazy``, this reliably produces detectable corruption or a
    ``TiffFileError``. This test asserts that IS what happens -- it is a control
    that must PASS to prove the harness is not blind to the hazard, not a
    regression test for production code (production code never shares a handle).

    Empirically (10 trials of 8 channels each, same parameters, standalone probe
    during development): 10/10 trials produced at least one corrupted or errored
    channel (74-92% of individual channel reads corrupted per trial), and every
    observed error was ``tifffile.TiffFileError``. A single run here is expected to
    reproduce that reliably; see task-5-report.md for the raw numbers.

    FIX ROUND 2: this used to catch a bare ``except Exception:``, which meant an
    ``AttributeError`` from a broken ``_defeat_tifffile_internal_lock`` (see that
    function's docstring) would have been swallowed and reported as "corruption
    detected" -- the negative control would PASS while proving nothing, exactly the
    defect this whole file exists to catch, reproduced in the harness itself. The
    except clause below now catches ONLY ``tifffile.TiffFileError`` -- the single
    exception type actually observed in every empirical trial. Any other exception
    (``_LockDefeatFailed``, ``threading.BrokenBarrierError``, a plain bug) now
    propagates OUT of this test uncaught, failing it visibly.
    ``test_negative_control_is_not_self_certifying_when_lock_defeat_breaks`` proves
    this directly.
    """
    _identity_basic(monkeypatch)
    _hold_after_seek(monkeypatch)
    channel_names = [f"CH{i}" for i in range(FAULT_N)]
    image_path, expected_stack = _write_stack(
        tmp_path, seed=101, n_channels=FAULT_N, h=FAULT_H, w=FAULT_W
    )

    barrier = threading.Barrier(FAULT_N, timeout=30)
    real_open_lazy = preprocess.open_lazy
    monkeypatch.setattr(
        preprocess,
        "open_lazy",
        _make_synced_open_lazy(real_open_lazy, barrier, share_handle=True),
    )

    out_path = tmp_path / "out_shared.ome.tiff"
    corrupted = False
    try:
        preprocess.preprocess_multichannel_image(
            image_path=str(image_path),
            channel_names=list(channel_names),
            output_path=str(out_path),
            skip_nuclear=False,
            n_workers=FAULT_N,
        )
        got_stack = tifffile.imread(str(out_path))
        corrupted = got_stack.shape != expected_stack.shape or not np.array_equal(
            got_stack, expected_stack
        )
    except tifffile.TiffFileError:
        # A worker-thread TiffFileError (e.g. "corrupted strip cannot be reshaped
        # from (...) to (...)") surfaces here via future.result() inside
        # preprocess_multichannel_image -- this IS the detected corruption. Every
        # other exception type is deliberately left uncaught (see FIX ROUND 2
        # above): it means the HARNESS broke, not that corruption was detected.
        corrupted = True

    assert corrupted, (
        "shared-handle negative control produced NO detectable corruption or "
        "error -- the harness would be blind to a regression to this design"
    )


def test_per_thread_handles_survive_the_identical_fault_injection(tmp_path, monkeypatch):
    """The real design (``_read_and_process_channel_lazy``, unmodified), under the
    EXACT SAME fault injection as the negative control above: internal tifffile
    lock defeated and seek/read window widened on every handle, and every worker's
    raw read forced through the same Barrier. The only variable that changes from
    the negative control is ``share_handle=False`` -- each worker opens its own
    handle. This must stay GREEN; if it goes red, the per-thread-handle design no
    longer isolates threads from each other.
    """
    _identity_basic(monkeypatch)
    _hold_after_seek(monkeypatch)
    channel_names = [f"CH{i}" for i in range(FAULT_N)]
    image_path, expected_stack = _write_stack(
        tmp_path, seed=102, n_channels=FAULT_N, h=FAULT_H, w=FAULT_W
    )

    barrier = threading.Barrier(FAULT_N, timeout=30)
    real_open_lazy = preprocess.open_lazy
    monkeypatch.setattr(
        preprocess,
        "open_lazy",
        _make_synced_open_lazy(real_open_lazy, barrier, share_handle=False),
    )

    out_path = tmp_path / "out_per_thread.ome.tiff"
    preprocess.preprocess_multichannel_image(
        image_path=str(image_path),
        channel_names=list(channel_names),
        output_path=str(out_path),
        skip_nuclear=False,
        n_workers=FAULT_N,
    )

    got_stack = tifffile.imread(str(out_path))
    assert got_stack.shape == expected_stack.shape
    assert np.array_equal(got_stack, expected_stack), (
        "per-thread-handle design produced corruption under the SAME fault "
        "injection the shared-handle negative control reliably fails under"
    )


def test_negative_control_is_not_self_certifying_when_lock_defeat_breaks(
    tmp_path, monkeypatch
):
    """FIX ROUND 2's actual proof: if ``_defeat_tifffile_internal_lock`` can no
    longer locate/verify tifffile's internal lock (simulating a future tifffile
    version restructuring the private ``chunk_store._mutable_mapping._filecache``
    chain it walks), that must surface as a genuine failure -- never be
    misread as "corruption detected, negative control PASSED".

    Before fix round 2, ``test_shared_handle_negative_control_is_actually_caught``
    caught this exact scenario with a bare ``except Exception: corrupted = True``,
    which would have swallowed the resulting ``AttributeError``/
    ``_LockDefeatFailed`` and reported PASS -- self-certifying, establishing
    nothing. This test breaks ``_defeat_tifffile_internal_lock`` deliberately and
    asserts the failure propagates all the way out of
    ``preprocess_multichannel_image`` uncaught, exactly as
    ``test_shared_handle_negative_control_is_actually_caught``'s narrowed
    ``except tifffile.TiffFileError`` (fix round 2) now requires: anything other
    than a real ``TiffFileError`` must NOT be absorbed.
    """
    _identity_basic(monkeypatch)
    _hold_after_seek(monkeypatch)
    channel_names = [f"CH{i}" for i in range(FAULT_N)]
    image_path, _expected_stack = _write_stack(
        tmp_path, seed=303, n_channels=FAULT_N, h=FAULT_H, w=FAULT_W
    )

    def broken_lock_defeat(arr):
        raise AttributeError(
            "simulated: a future tifffile restructured ZarrTiffStore's private "
            "internals, so this helper can no longer find the lock to defeat"
        )

    # Patches the SAME module-level name `_make_synced_open_lazy` resolves at call
    # time (it is a free variable, not captured early), so calls made later --
    # inside the worker threads `preprocess_multichannel_image` spins up -- go
    # through the broken version.
    monkeypatch.setattr(
        sys.modules[__name__], "_defeat_tifffile_internal_lock", broken_lock_defeat
    )

    barrier = threading.Barrier(FAULT_N, timeout=30)
    real_open_lazy = preprocess.open_lazy
    monkeypatch.setattr(
        preprocess,
        "open_lazy",
        _make_synced_open_lazy(real_open_lazy, barrier, share_handle=True),
    )

    out_path = tmp_path / "out_broken_harness.ome.tiff"
    with pytest.raises(AttributeError, match="simulated"):
        preprocess.preprocess_multichannel_image(
            image_path=str(image_path),
            channel_names=list(channel_names),
            output_path=str(out_path),
            skip_nuclear=False,
            n_workers=FAULT_N,
        )
