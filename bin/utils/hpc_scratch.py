"""Redirect $HOME-based caches to node-local scratch (HPC read-only $HOME fix).

On clusters like IEO's, ``$HOME`` (``/hpcnfs/...``) is mounted read-only on
compute nodes. Any library that defaults its cache/config under ``$HOME``
crashes the moment it tries to create that directory. We've already hit this
three ways:

* **numba** — JIT cache under ``~/.cache`` (handled inline in each entrypoint
  via ``NUMBA_CACHE_DIR`` / ``NUMBA_DISABLE_CACHING``).
* **matplotlib** — config under ``~/.config/matplotlib`` (warns, then falls
  back to /tmp on its own — non-fatal, but noisy).
* **cjdk** — the JDK fetcher ``scyjava``/``jgo`` use to start the bioformats
  JVM. It writes to ``~/.cache/cjdk`` and raises ``ConfigError`` if it can't,
  which is **fatal**: it kills ``registration.Valis(...)`` at ``init_jvm``.

Importing this module (BEFORE ``import valis``) points every offender at a
writable scratch dir. Caches are content-addressed, so sharing one scratch
base across concurrent tasks on the same node is safe. We honour ``$TMPDIR``
(SLURM/Nextflow set it to node-local scratch) and fall back to ``/tmp``.

Usage (mirrors the existing numba block — keep it before the valis import)::

    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils"))
    import hpc_scratch  # noqa: F401
    from valis import registration
"""
import os

_scratch = os.environ.get("TMPDIR", "/tmp")
_cache = os.path.join(_scratch, "mirage_cache")
_cjdk = os.path.join(_cache, "cjdk")
_mpl = os.path.join(_cache, "mpl")

# cjdk's mkdir does not create parents, so make the leaves ourselves.
for _d in (_cjdk, _mpl):
    os.makedirs(_d, exist_ok=True)

# cjdk reads CJDK_CACHE_DIR first, then falls back to XDG_CACHE_HOME, then
# ~/.cache. Set all the levers so neither path lands on read-only $HOME.
os.environ.setdefault("XDG_CACHE_HOME", _cache)
os.environ.setdefault("XDG_CONFIG_HOME", _cache)
os.environ.setdefault("CJDK_CACHE_DIR", _cjdk)
os.environ.setdefault("MPLCONFIGDIR", _mpl)
