#!/usr/bin/env python3
"""Guard: CI cache keys must be keyed on MANIFESTS, and each cached path must have
an owner in the file that caches it.

Three defects this repository actually shipped, all of them invisible in a green
run because a cache is only ever a download accelerator -- it never changes an
answer, only how long the answer takes:

  1. **A source-tree hash in a cache key.** The Nextflow distribution cache was
     keyed on ``${{ runner.os }}-nextflow-<version>-${{ hashFiles('**/*.nf') }}``.
     Not one byte of ``~/.nextflow`` is a function of this repository's ``.nf``
     files: it is the launcher and the framework jars for one Nextflow version.
     So the key MISSED on every commit that touched the pipeline -- i.e. on every
     run that matters -- and wrote a fresh, never-reused entry each time against
     the repository-wide 10 GB LRU budget, evicting the caches that did work.

  2. **A cached path with no owner in the caching file.** ``~/.nf-test`` was in
     that same cache's ``path:`` list, inside ``.github/actions/setup-nextflow``,
     which does not install nf-test. ``nf-core/setup-nf-test`` does -- and it
     caches ``~/.nf-test/nf-test`` and ``~/.nf-test/nf-test.jar`` ITSELF, under
     its own key ``nf-test-<version>-install-pdiff-<bool>`` (verified against
     ``dist/index.js`` at the pinned ``@v1``, 2026-09-01). Two entries for the
     same bytes, keyed on two different version strings: bump
     ``env.NFTEST_VERSION`` and their key misses while ours still hits, so their
     install runs ``fs.move(src, '~/.nf-test/nf-test')`` -- no ``overwrite:
     true`` -- onto a file we just restored, and fs-extra fails the step with
     "dest already exists". Every nf-test job in the repository goes red on an
     nf-test version bump.

  3. **Two cache steps under one key.** A cache entry is IMMUTABLE: the first
     writer under a key wins and every later save silently no-ops. Two steps
     caching different paths under one key is therefore a race, not a share --
     whichever job finishes first decides what everybody restores for the life of
     the entry.

SCOPE is ``tests/ci_actions.scanned_files()`` -- every workflow AND every local
composite action -- because both of the repository's own ``actions/cache`` steps
live in a composite action now, and a guard whose glob stops at
``.github/workflows/`` would examine zero cache steps and pass.

The rules these tests encode are written out in prose, once, in
``.github/actions/setup-nextflow/action.yml``. Both of the "must be in the table"
checks below are deliberately CLOSED: a new cached path or a new hashed glob
fails until someone writes down what it is for. That is the point -- every
enumeration written on this project has been short, and a cache defect has no
symptom to notice.

Plain pytest, paths derived from ``__file__``, per this directory's other guards.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent

if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))
ci_actions = importlib.import_module("ci_actions")


# ---------------------------------------------------------------------------
# The two tables. Both CLOSED: anything absent from them fails.
# ---------------------------------------------------------------------------

# Every cached path declares TWO things, and both are checked:
#
#   installer        -- a substring that must appear in the SAME file. A path may
#                       be cached only where something puts the bytes there.
#                       `~/.nf-test` had no such owner in setup-nextflow, which is
#                       defect 2 above.
#   key_must_contain -- a substring that must appear in that cache step's `key:`.
#                       This is the OTHER half of rule 2, and it is the half a
#                       reviewer broke straight through on 2026-09-01: deleting
#                       `${{ inputs.nextflow-version }}` from the distribution key
#                       left `${{ runner.os }}-${{ runner.arch }}-nextflow` and all
#                       five cases here passed. The `25.04.0` and
#                       `latest-everything` legs would then share ONE immutable
#                       entry, and whichever saved first would fix its framework
#                       directory on the other for the life of the key. A key must
#                       name what determines the bytes it covers.
CACHED_PATH_OWNERS = {
    "~/.nextflow": (
        "nf-core/setup-nextflow",
        "inputs.nextflow-version",
        "the Nextflow launcher + framework jars, written by the install step; the "
        "VERSION is the whole of what determines them, so the key must name it",
    ),
    "${{ github.workspace }}/.nextflow-plugins": (
        "nextflow plugin install",
        "hashFiles('nextflow.config')",
        "NXF_PLUGINS_DIR, filled by the plugin provisioning step; nextflow.config "
        "declares the pin, so its hash is what determines the contents",
    ),
}

# Every glob whose CONTENT is hashed into a cache key. Rule 1: manifests only.
# `hashFiles('**/*.nf')` is not here, and adding it back is the failure this
# guard exists to produce.
HASHED_GLOBS = {
    "nextflow.config": (
        "the file that DECLARES the nf-schema plugin version -- a manifest. It "
        "rotates the plugin cache exactly when the pin changes."
    ),
    "requirements/**": (
        "the Python pin files, read by CI and by every container image alike -- "
        "a manifest tree. `cache: 'pip'` would otherwise default to "
        "`**/requirements.txt`, which matches only the root shim and "
        "docs/requirements.txt and never rotates when a pin changes."
    ),
}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
_HASHFILES_RE = re.compile(r"hashFiles\(\s*'([^']*)'\s*\)")


def _steps_of_file(path: Path):
    """Every step in a file, workflow or composite action, as (where, step)."""
    data = yaml.safe_load(path.read_text()) or {}
    rel = path.relative_to(ROOT).as_posix()
    out = []
    for job_id, job in (data.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict):
                out.append((f"{rel}::{job_id}", step))
    for step in ((data.get("runs") or {}).get("steps") or []):
        if isinstance(step, dict):
            out.append((rel, step))
    return out


def cache_steps():
    """Every `actions/cache` step across workflows and composite actions."""
    found = []
    for path in ci_actions.scanned_files():
        for where, step in _steps_of_file(path):
            if "actions/cache" in str(step.get("uses", "")):
                found.append((path, where, step))
    return found


def _uncommented(text: str) -> str:
    """`text` with whole-line YAML comments removed.

    The owner check below is a substring search, and this file's own header
    DISCUSSES `nf-core/setup-nf-test` at length -- so a search over the raw text
    would find the owner of `~/.nf-test` in the very file that must not cache it,
    and the check would go green over the defect it exists to catch. Verified by
    doing it: with the raw text the "declared but in the wrong file" case passed.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


def _lines(value) -> list[str]:
    """A `path:`/`restore-keys:` value, single-line or block scalar, as a list."""
    return [
        line.strip()
        for line in str(value or "").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def hashed_globs():
    """Every glob whose content ends up in a cache key, as (where, field, glob).

    Two forms, and this repo writes both:

      * `hashFiles('<glob>')` inside an `actions/cache` `key:`/`restore-keys:`;
      * `cache-dependency-path:` (actions/setup-python) or
        `cache-dependency-glob:` (astral-sh/setup-uv), which the action hashes
        into a key it builds itself. Leaving these out would let the SAME defect
        move one step sideways and go unseen.
    """
    out = []
    for path, where, step in cache_steps():
        with_block = step.get("with") or {}
        for field in ("key", "restore-keys"):
            for glob in _HASHFILES_RE.findall(str(with_block.get(field, ""))):
                out.append((where, field, glob))
    for path in ci_actions.scanned_files():
        for where, step in _steps_of_file(path):
            with_block = step.get("with") or {}
            for field in ("cache-dependency-path", "cache-dependency-glob"):
                for glob in _lines(with_block.get(field)):
                    out.append((where, field, glob))
    return out


# ---------------------------------------------------------------------------
# Non-vacuity: every check below is a "must not find" scan.
# ---------------------------------------------------------------------------
def test_the_scan_found_the_caches_this_repository_has():
    """A scan that reads zero cache steps passes every rule below having checked
    nothing -- and the caches are all in composite actions now, one glob away
    from being invisible."""
    steps = cache_steps()
    assert len(steps) >= 2, (
        f"found {len(steps)} `actions/cache` step(s) across "
        f"{[p.relative_to(ROOT).as_posix() for p in ci_actions.scanned_files()]}. "
        "This repository caches the Nextflow distribution and the Nextflow plugins "
        "directory; if that is no longer true, delete this file with them, and if it "
        "is, the `uses: actions/cache` match or the file scope broke."
    )
    globs = hashed_globs()
    assert len(globs) >= 2, (
        f"found {len(globs)} hashed glob(s) ({globs}). The plugin cache hashes "
        "nextflow.config and the pip cache hashes requirements/**; a smaller number "
        "means this scan is not reading the keys it thinks it is."
    )


# ---------------------------------------------------------------------------
# Rule 1: hashFiles() targets manifests, never a source tree.
# ---------------------------------------------------------------------------
def test_no_cache_key_hashes_anything_but_a_declared_manifest():
    """A source-tree hash guarantees a MISS on every commit that matters.

    It is not a neutral mistake. The entry is still SAVED, so every such run adds
    a fresh never-reused entry to a 10 GB repository-wide LRU and evicts the
    caches that do get hits. `hashFiles('**/*.nf')` in the Nextflow distribution
    key was doing exactly that until 2026-09-01.

    The table is closed on purpose: a new glob fails here until someone writes
    down why it is a manifest.
    """
    offenders = []
    for where, field, glob in hashed_globs():
        if glob not in HASHED_GLOBS:
            offenders.append(f"{where}: `{field}` hashes {glob!r}")
    assert not offenders, (
        "a cache key hashes a glob that is not a declared manifest:\n  "
        + "\n  ".join(offenders)
        + "\n\nhashFiles() targets MANIFESTS (requirements/*, nextflow.config, a "
        "lockfile), never a source tree: a source hash misses on every commit that "
        "changes the thing being built and writes a fresh, never-reused entry each "
        "time. If this really is a manifest, add it to HASHED_GLOBS with the reason.\n"
        "Declared manifests: " + ", ".join(sorted(HASHED_GLOBS))
    )


# ---------------------------------------------------------------------------
# Rule 2, first half: a cached path must have an installer in the same file.
# ---------------------------------------------------------------------------
def test_every_cached_path_has_a_declared_installer_in_the_same_file():
    """Caching a directory that nothing in the same file creates is how
    ``~/.nf-test`` came to be cached by ``setup-nextflow``, twice over and under
    the wrong key -- see defect 2 in this module's docstring.

    "In the same file" is the operative half. ``nf-core/setup-nf-test`` DOES
    install ``~/.nf-test``, but it lives in a different composite action, so a
    cache of that path in ``setup-nextflow`` is unowned there and fails here even
    if someone adds a CACHED_PATH_OWNERS entry for it.
    """
    offenders = []
    checked = 0
    for path, where, step in cache_steps():
        text = _uncommented(path.read_text())
        for cached in _lines((step.get("with") or {}).get("path")):
            checked += 1
            if cached not in CACHED_PATH_OWNERS:
                offenders.append(
                    f"{where}: caches {cached!r}, which is not in CACHED_PATH_OWNERS"
                )
                continue
            owner, _key_component, reason = CACHED_PATH_OWNERS[cached]
            if owner not in text:
                offenders.append(
                    f"{where}: caches {cached!r} but {owner!r} ({reason}) does not "
                    f"appear in {path.relative_to(ROOT).as_posix()}, so nothing in "
                    "that file puts the bytes there"
                )
    assert checked >= 2, f"only {checked} cached path(s) examined; the scan is not reading them"
    assert not offenders, (
        "cached path(s) with no owner in the file that caches them:\n  "
        + "\n  ".join(offenders)
        + "\n\nCache the directory where it is INSTALLED, or not at all. A second "
        "entry for bytes another action already caches is keyed on the wrong thing "
        "and eventually restores a file that action then refuses to overwrite."
    )


# ---------------------------------------------------------------------------
# Rule 2, second half: one key, one cache.
# ---------------------------------------------------------------------------
def test_no_two_cache_steps_share_a_key():
    """A cache entry is immutable: first writer wins, later saves silently no-op.

    Two steps caching different paths under one key template is therefore a race
    whose outcome is whichever job finished first -- and it is silent, because
    both steps report a cache hit forever after.
    """
    by_key: dict[str, list[str]] = {}
    for _path, where, step in cache_steps():
        key = " ".join(str((step.get("with") or {}).get("key", "")).split())
        assert key, f"{where}: `actions/cache` step with no `key:` at all"
        by_key.setdefault(key, []).append(where)
    clashes = {k: v for k, v in by_key.items() if len(v) > 1}
    assert not clashes, (
        "cache step(s) share a key template:\n  "
        + "\n  ".join(f"{k!r}: {v}" for k, v in sorted(clashes.items()))
        + "\n\nThe first save under a key wins and every later one silently no-ops, "
        "so the two caches are not sharing -- one of them is being discarded."
    )


# ---------------------------------------------------------------------------
# restore-keys says whether the key can miss.
# ---------------------------------------------------------------------------
def test_restore_keys_exist_exactly_where_the_key_can_miss():
    """A prefix fallback is meaningful for one kind of key and pointless for the
    other, and this repository has one of each.

    * A key carrying a CONTENT HASH (the plugins cache, hashing nextflow.config)
      misses whenever the manifest changes, which is precisely when a warm start
      is still mostly valid -- so it needs a prefix to fall back to.
    * A key fully determined by a VERSION STRING (the Nextflow distribution)
      either hits or the version was bumped, and a bumped version wants a fresh
      download, not the previous version's jars. A prefix there restores the very
      entry the key already names, or something deliberately superseded.

    Stated as a rule rather than as two facts so that the next cache added has to
    answer the question.
    """
    offenders = []
    for _path, where, step in cache_steps():
        with_block = step.get("with") or {}
        key = str(with_block.get("key", ""))
        has_hash = bool(_HASHFILES_RE.search(key))
        has_fallback = bool(_lines(with_block.get("restore-keys")))
        if has_hash and not has_fallback:
            offenders.append(
                f"{where}: key hashes a manifest but declares no `restore-keys:`, so "
                "every pin change is a fully cold start"
            )
        if not has_hash and has_fallback:
            offenders.append(
                f"{where}: key has no content hash, so it hits or the version moved -- "
                "a `restore-keys:` prefix can only restore the entry the key already "
                "names, or a superseded one"
            )
    assert not offenders, "\n".join(offenders)


def test_every_cached_path_is_keyed_on_what_determines_its_contents():
    """The other half of rule 2, and the half that was missing.

    A cache entry is immutable, so a key that does not name everything the cached
    bytes depend on quietly merges two different things into one entry and hands
    the loser whatever the winner saved. `test_no_two_cache_steps_share_a_key`
    only catches two SEPARATE steps colliding; this catches ONE step whose key has
    lost a component and now collides with itself across matrix legs.

    Measured, not hypothetical: on 2026-09-01 a reviewer deleted
    `${{ inputs.nextflow-version }}` from the distribution key and every case in
    this file still passed, while `25.04.0` and `latest-everything` would have
    shared one entry.
    """
    offenders = []
    checked = 0
    for _path, where, step in cache_steps():
        with_block = step.get("with") or {}
        key = str(with_block.get("key", ""))
        for cached in _lines(with_block.get("path")):
            if cached not in CACHED_PATH_OWNERS:
                continue  # test_every_cached_path_has_a_declared_installer... owns this
            _owner, component, reason = CACHED_PATH_OWNERS[cached]
            checked += 1
            if component not in key:
                offenders.append(
                    f"{where}: caches {cached!r} under key {key!r}, which does not "
                    f"contain {component!r} -- {reason}"
                )
    assert checked >= 2, (
        f"only {checked} cached path(s) had a declared key component; the scan is "
        "not reading them"
    )
    assert not offenders, (
        "cache key(s) do not name what determines the bytes they cover:\n  "
        + "\n  ".join(offenders)
        + "\n\nA cache entry is IMMUTABLE. A key missing a component merges two "
        "different caches into one entry and the first writer wins -- silently, "
        "and forever, because both steps report a hit afterwards."
    )


def test_every_workflow_that_installs_nextflow_pins_nxf_plugins_dir():
    """`NXF_PLUGINS_DIR` is load-bearing for the DISTRIBUTION cache, not just a
    speed knob, and nothing asserted it.

    With the distribution key reduced to os/arch/version (Phase 3), `~/.nextflow`
    is claimed to be a pure function of the Nextflow version. That is only true
    while the plugins live somewhere else. A workflow that forgot the `env:` would
    let nf-schema land in `~/.nextflow/plugins`, and the plugin set would be
    frozen into a version-only entry that never rotates when `nextflow.config`
    changes the pin -- the exact staleness the separate plugins cache exists to
    avoid, reintroduced through the back door and invisible.

    Scope is every JOB that reaches `.github/actions/setup-nextflow`, resolved
    through `tests/ci_actions.py`, so a job that picks it up via `setup-nf-test`
    counts too. `env:` may be declared at workflow or job level; both work.
    """
    var = "NXF_PLUGINS_DIR"
    offenders = []
    checked = 0
    for path in ci_actions.workflow_files():
        data = yaml.safe_load(path.read_text()) or {}
        workflow_env = data.get("env") or {}
        for job_id, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            actions = {p.parent.name for p in ci_actions.job_local_actions(job)}
            if "setup-nextflow" not in actions:
                continue
            checked += 1
            job_env = job.get("env") or {}
            if var not in workflow_env and var not in job_env:
                offenders.append(
                    f"{path.relative_to(ROOT).as_posix()}::{job_id} installs Nextflow "
                    f"but neither it nor its workflow sets {var}"
                )
    assert checked >= 3, (
        f"only {checked} job(s) were found to install Nextflow. ci.yml, release.yml "
        "and nightly.yml all do, several times over; a number this small means the "
        "resolver stopped reaching .github/actions/setup-nextflow."
    )
    assert not offenders, (
        f"{var} must be set wherever Nextflow is installed -- without it the plugins "
        "land inside ~/.nextflow and get frozen into a cache key that only names the "
        "Nextflow version:\n  " + "\n  ".join(offenders)
    )
