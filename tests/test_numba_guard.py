"""Regression guard for the HPC read-only-filesystem numba caching crash.

On clusters where $HOME lives on a read-only mount (e.g. /hpcnfs), importing
valis transitively imports the `interpolation` package, whose `@njit(cache=True)`
functions make numba attempt an on-disk cache. With no writable cache location
numba raises:

    RuntimeError: cannot cache function '_repeat_1d': no locator available ...

register.py avoids this by setting NUMBA_DISABLE_CACHING / NUMBA_CACHE_DIR
*before* importing valis. Every script that imports valis as a module-level
entrypoint must do the same, BEFORE the first valis import in source order.

This is a static source test (no valis install required) so it runs in CI.
"""
import re
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

# Module-level entrypoints that import valis (directly or via valis_config) at
# load time and therefore need the numba-cache guard before the first valis import.
ENTRYPOINTS = [
    "register.py",      # positive control: already guarded
]

# Matches a top-level import that triggers valis (and thus numba) at load time.
# `from valis_config import ...` also matches `^from valis` on purpose: that
# util imports valis at module top, so the guard must precede it too.
VALIS_IMPORT = re.compile(r"^\s*(from valis|import valis\b)")
GUARD = re.compile(r"NUMBA_DISABLE_CACHING|NUMBA_CACHE_DIR")


@pytest.mark.parametrize("script", ENTRYPOINTS)
def test_numba_guard_precedes_valis_import(script):
    lines = (BIN / script).read_text().splitlines()

    first_valis = next(
        (i for i, ln in enumerate(lines) if VALIS_IMPORT.match(ln)), None
    )
    assert first_valis is not None, f"{script}: no valis import found"

    first_guard = next((i for i, ln in enumerate(lines) if GUARD.search(ln)), None)
    assert first_guard is not None, (
        f"{script}: missing NUMBA cache guard — read-only-FS clusters will crash "
        f"in numba caching on valis import"
    )
    assert first_guard < first_valis, (
        f"{script}: NUMBA guard at line {first_guard + 1} must come before the "
        f"first valis import at line {first_valis + 1}"
    )
