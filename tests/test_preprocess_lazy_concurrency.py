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
Fix round 2 ALSO added a test named ``test_negative_control_is_not_self_certifying_when_lock_defeat_breaks``,
believing it proved the narrowing above. It did not, and was DELETED in fix round
3 (see below) -- it called ``preprocess.preprocess_multichannel_image`` directly
and asserted ``pytest.raises(AttributeError)`` around its OWN call, never actually
routing through ``test_shared_handle_negative_control_is_actually_caught``'s
try/except at all. Reverting that test's except clause back to bare ``Exception``
left the "proof" test passing unchanged, because nothing in it depended on the
except clause under test. See "FIX ROUND 3" below for the actual fix.

Also recorded (not fixable here): the negative control's discriminating power
depends entirely on ``_filecache.lock`` remaining tifffile's SOLE synchronization
point for concurrent ``ZarrTiffStore`` reads. A future tifffile version that adds a
SECOND, different synchronization mechanism alongside or instead of
``_filecache.lock`` would leave this control green-by-luck (defeating the wrong /
an incomplete lock), not red. This control demonstrates the shared-vs-per-thread
DESIGN difference under today's tifffile internals; it is not a permanent
guarantee against every future tifffile locking strategy.

FIX ROUND 3 -- the round-2 "proof" test proved nothing; replaced with a real one
---------------------------------------------------------------------------
A re-review reverted ``test_shared_handle_negative_control_is_actually_caught``'s
except clause back to bare ``except Exception:`` and re-ran
``test_negative_control_is_not_self_certifying_when_lock_defeat_breaks``: it STILL
PASSED. That test never exercised the try/except it claimed to be proving safe --
it drove ``preprocess_multichannel_image`` directly and asserted on its own
``pytest.raises``, and ``bin/preprocess.py`` has no try/except around
``future.result()`` at all, so that ``AttributeError`` was always going to
propagate regardless of anything fix round 2 changed. Same shape of defect as the
two before it in this file's history: an artifact that reads as evidence and
establishes nothing.

Fixed by extracting the classification decision itself into a small,
directly-testable function, ``_is_hazard_signal(exc) -> bool``, and having
``test_shared_handle_negative_control_is_actually_caught`` call it (rather than a
static ``except tifffile.TiffFileError:`` written once in the test and never
exercised by anything else). ``test_is_hazard_signal_classifies_a_real_corruption_error_as_a_hazard``
/ ``..._does_not_classify_a_broken_lock_defeat_as_a_hazard`` /
``..._does_not_classify_a_bare_attribute_error_as_a_hazard`` unit-test the helper
directly and exhaustively, with no threading, no tifffile internals, no fault
injection at all -- just the three exception shapes that matter. Widening
``_is_hazard_signal`` back to "everything is a hazard" (the round-2 regression,
reintroduced) now fails those three assertions immediately and locally; see
task-5-report.md's "Fix round 3" section for the verbatim RED (widened) and GREEN
(restored) transcript. ``test_negative_control_is_not_self_certifying_when_lock_defeat_breaks``
was DELETED rather than patched, per the instruction that a test whose name
asserts something it does not check is worse than not having it.
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
# Fault-injected negative control (fix round 1; hardened in fix rounds 2 and 3 --
# see the module docstring's "FIX ROUND 1" / "FIX ROUND 2" / "FIX ROUND 3" sections).
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


def _is_hazard_signal(exc: BaseException) -> bool:
    """True iff ``exc`` represents a genuinely corrupted/erroring read from the
    fault-injected negative control -- the thing
    ``test_shared_handle_negative_control_is_actually_caught`` exists to detect.

    Currently that is ``tifffile.TiffFileError`` ONLY -- the sole exception type
    observed across every empirical corruption trial (e.g.
    ``TiffFileError('corrupted strip cannot be reshaped from (...) to (...)')``,
    see task-5-report.md's fix-round-1 raw numbers). Any other exception --
    ``_LockDefeatFailed`` (the negative control's OWN harness breaking),
    ``AttributeError`` (an unguarded private-attribute chain breaking),
    ``threading.BrokenBarrierError``, a plain bug -- means the HARNESS broke, not
    that a hazard was detected, and must classify as False here.

    This is a free-standing, directly unit-tested function (see the
    ``test_is_hazard_signal_*`` tests below) SPECIFICALLY so that widening it back
    to "everything counts as a hazard" -- the exact regression fix round 2
    introduced and fix round 3 removed (see the module docstring's "FIX ROUND 3"
    section) -- breaks a fast, local, non-threaded assertion immediately, instead
    of depending on an end-to-end concurrent run to happen to notice.
    ``test_shared_handle_negative_control_is_actually_caught`` calls this function
    directly (not a static ``except tifffile.TiffFileError:`` clause written twice)
    so the two cannot silently drift apart.
    """
    return isinstance(exc, tifffile.TiffFileError)


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


# ---------------------------------------------------------------------------
# Direct, exhaustive unit tests of the classification decision itself (fix round
# 3). These are the actual proof that the negative control cannot silently
# self-certify again: they exercise `_is_hazard_signal` with no threading, no
# tifffile internals, no fault injection -- just the three exception shapes that
# matter -- so a widened ("everything is a hazard") implementation fails one of
# these immediately and locally, without requiring an end-to-end concurrent run to
# happen to notice.
# ---------------------------------------------------------------------------


def test_is_hazard_signal_classifies_a_real_corruption_error_as_a_hazard():
    assert _is_hazard_signal(tifffile.TiffFileError("corrupted strip")) is True


def test_is_hazard_signal_does_not_classify_a_broken_lock_defeat_as_a_hazard():
    """_LockDefeatFailed means the NEGATIVE CONTROL's own harness broke (see its
    docstring) -- not that a hazard was detected."""
    assert _is_hazard_signal(_LockDefeatFailed("could not locate the lock")) is False


def test_is_hazard_signal_does_not_classify_a_bare_attribute_error_as_a_hazard():
    """The exact scenario fix round 2 was opened to stop being swallowed: an
    unguarded private-attribute chain breaking (e.g. a future tifffile
    restructuring ``chunk_store._mutable_mapping._filecache``) raises
    AttributeError, which must NOT read as 'corruption detected'."""
    assert _is_hazard_signal(AttributeError("no such attribute")) is False


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

    FIX ROUND 2 narrowed this test's except clause from bare ``Exception`` to
    ``tifffile.TiffFileError`` -- catching an ``AttributeError`` from a broken
    ``_defeat_tifffile_internal_lock`` as "corruption detected" was exactly the
    self-certification defect this whole file exists to catch, reproduced in the
    fix meant to catch it. But narrowing the except clause IN THIS TEST is not, by
    itself, something a future regression could be caught reintroducing: the
    original round-2 "proof" test called this function directly and asserted on
    its OWN ``pytest.raises``, never actually exercising the try/except below --
    reverting the except clause back to bare ``Exception`` left that "proof" test
    passing unchanged. FIX ROUND 3 fixes that by extracting the classification
    decision into ``_is_hazard_signal`` (see its docstring) and calling it here,
    with the ``test_is_hazard_signal_*`` tests directly, exhaustively unit-testing
    the classification with no threading involved. Widening ``_is_hazard_signal``
    back to "everything is a hazard" now fails those unit tests immediately.
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
    except Exception as exc:
        if not _is_hazard_signal(exc):
            # The HARNESS broke (e.g. _LockDefeatFailed, a plain bug) -- not a
            # detected hazard. Must not be absorbed; let it fail this test loudly.
            raise
        # A worker-thread TiffFileError (e.g. "corrupted strip cannot be reshaped
        # from (...) to (...)") surfaces here via future.result() inside
        # preprocess_multichannel_image, and _is_hazard_signal confirms it IS the
        # detected corruption.
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
