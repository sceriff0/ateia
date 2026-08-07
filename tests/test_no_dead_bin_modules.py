"""Guard test: every bin/utils/*.py module must have a real importer.

This repo uses two import conventions for `bin/utils/*.py` (because many
scripts do `sys.path.insert(0, .../bin/utils)` before importing):

- flat:      `from image_utils import ensure_dir`
- package:   `from utils.metadata import ...`, `from utils import cell_pairs as cp`
- relative:  `from .gmm import fit_two_component_gmm` (within bin/utils/phenotyping/)

A naive "does the bare word 'tiling' appear anywhere" scan produces false
negatives for short names (`qc`, `retry`, `validation` all appear constantly
in prose/comments/unrelated identifiers) and false positives are just as
easy: it would call a module "used" because its own name shows up in a
sentence. This test instead parses every candidate file with `ast` and only
counts genuine `import`/`from ... import ...` statements, resolved against
each module's basename and its dotted path under `utils.`.

bin/utils/__init__.py is deliberately excluded from the set of files this
test treats as "importers". Historically it barrel re-exported `cli.py`
(`create_base_parser`, `setup_logging_from_args`, `add_input_output_args`)
and `constants.py` (`ExitCode`, `DEFAULT_FOV_SIZE`, etc.) even though
nothing downstream ever consumed those re-exported names (verified by
exhaustive grep across bin/, tests/, modules/, conf/, docs/, and
containers/ for every symbol in both modules). Counting that barrel import
as "usage" would have hidden both modules' deadness behind their own
package's re-export statement -- the exact false-negative this exclusion
exists to prevent. Both modules have since been deleted (see git history);
__init__.py now only re-exports `logger.py`, which is independently alive
via direct imports elsewhere (see `test_known_alive_modules_are_not_false_
flagged`). Modules used elsewhere via `from logger import ...`, `from
metadata import ...`, etc. are unaffected, since those imports come
directly from consumer scripts, not through the barrel.

Known latent limitation: because __init__.py is excluded from the haystack,
a *future* module consumed *exclusively* via `from utils import
<exported_name>` -- i.e. only through a barrel-exported symbol, never by
its own module path or basename -- would be misclassified as dead. The
matcher recognizes `from utils import <module_name>` as proof of life
(e.g. `from utils import cell_pairs`), but has no way to trace `from utils
import <a_symbol_defined_inside_that_module>` back to the module that
defines it, since the symbol's name need not match the module's basename
(e.g. `get_logger` vs. `logger.py`). Not currently triggered -- verified:
`logger.py` is alive via direct imports (`from utils.logger import ...` /
`from logger import ...`) independent of the barrel, and no other
bin/utils module is consumed exclusively through a barrel-exported symbol.
A future module that *is* exposed and consumed only that way would need
either a direct import somewhere or an ALLOWLIST entry to avoid a false
"dead" flag.
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
UTILS_DIR = BIN_DIR / "utils"
TESTS_DIR = REPO_ROOT / "tests"

# bin/utils/__init__.py's barrel re-import doesn't count as genuine usage --
# see module docstring above.
HAYSTACK_EXCLUDE = {UTILS_DIR / "__init__.py"}

# Modules with zero real importers that are intentionally kept anyway.
#
# Map of basename -> {"status": ..., "reason": ...}. `status` is structural,
# not prose, so a machine (and a future reader skimming, not reading) can
# tell the two fundamentally different kinds of entry apart:
#
#   "permanent"        -- deliberate, standing policy. Keep indefinitely.
#   "pending_removal"  -- temporary debt: the module is dead by this test's
#                         own standard, deletion is just deferred. Without a
#                         structural marker, "temporary" silently becomes
#                         "permanent" because nobody re-reads the prose.
#                         See test_pending_removal_entries_are_flagged_loudly:
#                         any entry with this status is surfaced as a pytest
#                         warning on every run, not just skipped quietly.
_ALLOWED_STATUSES = {"permanent", "pending_removal"}

ALLOWLIST: dict[str, dict[str, str]] = {}


def _candidate_modules() -> list[Path]:
    """bin/utils/**/*.py, excluding __init__.py."""
    mods = []
    for path in sorted(UTILS_DIR.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        mods.append(path)
    return mods


def _haystack_files() -> list[Path]:
    """Every .py file under bin/ or tests/ that could plausibly import a module."""
    files = list(BIN_DIR.rglob("*.py")) + list(TESTS_DIR.rglob("*.py"))
    return [f for f in files if f not in HAYSTACK_EXCLUDE]


def _dotted_parts(module_path: Path) -> tuple[str, ...]:
    """Dotted path parts relative to bin/utils, e.g. ('phenotyping', 'gmm')."""
    rel = module_path.relative_to(UTILS_DIR).with_suffix("")
    return rel.parts


def _imported_leaf_names(py_file: Path) -> set[str]:
    """All module leaf-names this file genuinely imports (any real import form)."""
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    except (SyntaxError, UnicodeDecodeError):
        return set()

    leaves: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # import tiling / import utils.tiling / import utils.phenotyping.gmm
            for alias in node.names:
                leaves.add(alias.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod_parts = node.module.split(".")
                # from tiling import X / from utils.tiling import X /
                # from .tiling import X / from ..phenotyping.gmm import X
                leaves.add(mod_parts[-1])
                # from utils import cell_pairs / from utils.phenotyping import gmm:
                # the package itself is named, and the real submodule reference
                # is one of the imported names.
                if mod_parts[-1] in ("utils", "phenotyping"):
                    for alias in node.names:
                        leaves.add(alias.name)
            else:
                # from . import cell_pairs  (level > 0, no module)
                for alias in node.names:
                    leaves.add(alias.name)
    return leaves


def _is_imported_anywhere(module_path: Path, haystack: list[Path]) -> bool:
    basename = module_path.stem
    dotted_leaf = _dotted_parts(module_path)[-1]
    assert basename == dotted_leaf
    for f in haystack:
        if f == module_path:
            continue
        if basename in _imported_leaf_names(f):
            return True
    return False


CANDIDATES = _candidate_modules()


@pytest.mark.parametrize(
    "module_path",
    CANDIDATES,
    ids=[str(p.relative_to(UTILS_DIR)) for p in CANDIDATES],
)
def test_bin_utils_module_has_a_real_importer(module_path: Path) -> None:
    basename = module_path.stem
    if basename in ALLOWLIST:
        entry = ALLOWLIST[basename]
        pytest.skip(f"allowlisted ({entry['status']}): {entry['reason']}")

    haystack = _haystack_files()
    assert _is_imported_anywhere(module_path, haystack), (
        f"bin/utils/{module_path.relative_to(UTILS_DIR)} is not imported by any real "
        "import statement (flat, package, or relative form) under bin/ or tests/. "
        "If it's genuinely dead, delete it; if it should be kept, add it to "
        "ALLOWLIST in this test with a reason."
    )


def test_known_alive_modules_are_not_false_flagged() -> None:
    """Sanity check the matcher itself against modules known to be genuinely used."""
    haystack = _haystack_files()
    # logger.py is the one remaining barrel-exported-but-genuinely-alive
    # module: bin/utils/__init__.py re-exports get_logger/configure_logging/
    # etc., but this test must confirm the matcher finds it alive through
    # *direct* imports (`from utils.logger import ...` / `from logger import
    # ...`), independent of that barrel -- exactly the case the module
    # docstring's design rationale (and its documented latent limitation)
    # argues about.
    for name in ("image_utils", "validation", "metadata", "cell_pairs", "logger"):
        candidates = [p for p in CANDIDATES if p.stem == name]
        assert candidates, f"expected to find bin/utils/{name}.py among candidates"
        assert _is_imported_anywhere(candidates[0], haystack), (
            f"matcher false-negatived a module known to be alive: {name}"
        )


def test_allowlist_entries_declare_a_valid_status() -> None:
    """Every ALLOWLIST entry must be a structured {status, reason} dict.

    This is what makes "permanent" and "pending_removal" a real distinction
    instead of a convention someone can forget: a bare string reason (the
    old format) or an unrecognized status value fails collection outright,
    forcing whoever defers a module next to explicitly pick a kind.
    """
    for name, entry in ALLOWLIST.items():
        assert isinstance(entry, dict) and set(entry) == {"status", "reason"}, (
            f"ALLOWLIST[{name!r}] must be a dict with exactly 'status' and "
            "'reason' keys, not a bare string -- see the ALLOWLIST comment."
        )
        assert entry["status"] in _ALLOWED_STATUSES, (
            f"ALLOWLIST[{name!r}] has status={entry['status']!r}; must be "
            f"one of {sorted(_ALLOWED_STATUSES)}."
        )


def test_pending_removal_entries_are_flagged_loudly() -> None:
    """`pending_removal` entries must not sit in ALLOWLIST silently.

    A `pending_removal` entry marks temporary debt -- a module this test
    would otherwise flag as dead, with deletion deferred to a later task --
    as opposed to a `permanent` entry, which is deliberate standing policy
    (e.g. a module deliberately kept for a documented external consumer).
    Deferring is a legitimate outcome, so this test doesn't fail merely
    because one exists. But it must not be
    possible to defer something and have it quietly age into permanence:
    every `pending_removal` entry is re-emitted as a pytest warning on
    *every* run, so it shows up in the warnings summary (which CI logs and
    reviewers actually scan) instead of disappearing into ALLOWLIST prose
    nobody re-reads once it's merged.
    """
    pending = {
        name: entry
        for name, entry in ALLOWLIST.items()
        if entry["status"] == "pending_removal"
    }
    for name, entry in pending.items():
        warnings.warn(
            f"ALLOWLIST['{name}'] is status=pending_removal (temporary debt, "
            f"not permanent policy) -- deletion is still owed: {entry['reason']}",
            stacklevel=1,
        )
