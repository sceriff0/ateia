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
test treats as "importers". It re-exports `create_base_parser`,
`setup_logging_from_args`, `add_input_output_args` (from cli.py) and
`ExitCode`, `DEFAULT_FOV_SIZE`, etc. (from constants.py) via `from .cli
import (...)` / `from .constants import (...)`, but nothing downstream ever
consumes those re-exported names (verified by exhaustive grep across bin/
and tests/ for every symbol in both modules). Counting that barrel import as
"usage" would hide cli.py's deadness behind its own package's re-export
statement -- the exact false-negative this test exists to prevent. Modules
that are used elsewhere via `from logger import ...`, `from metadata import
...`, etc. are unaffected, since those imports come directly from consumer
scripts, not through the barrel.
"""

from __future__ import annotations

import ast
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
# Map of basename -> reason.
ALLOWLIST = {
    "reg_benchmark": (
        "bin/utils/reg_benchmark.py is the dependency of "
        "bin/registration_benchmark.py, an unreferenced manual harness "
        "documented in docs/parallel_registration_design.md. A sibling "
        "`benchmarking` branch may depend on it (task-4 brief, explicit "
        "do-not-delete list). Not a false negative in this test -- it is "
        "genuinely unimported by anything under bin/ or tests/, and is kept "
        "on purpose."
    ),
    "constants": (
        "bin/utils/constants.py is only referenced by bin/utils/__init__.py's "
        "own barrel re-export (`from .constants import (...)`), which is "
        "excluded from this test's haystack -- same pattern as cli.py. "
        "Exhaustive grep confirms every exported symbol (ExitCode, "
        "DEFAULT_FOV_SIZE, DEFAULT_PIXEL_SIZE, DEFAULT_TILE_SIZE, "
        "LOG_SEPARATOR, TIFF_EXTENSIONS, OME_TIFF_EXTENSIONS, ...) has zero "
        "downstream consumers, i.e. it is genuinely dead by the same "
        "standard as the three modules this task deletes. It is out of "
        "scope for task-4 (not named in the brief) and is left in place "
        "pending a follow-up task -- allowlisted here rather than silently "
        "hidden by excluding __init__.py from the haystack."
    ),
}


def _candidate_modules() -> list[Path]:
    """bin/utils/**/*.py, excluding __init__.py and the vendored cse/ tree."""
    mods = []
    for path in sorted(UTILS_DIR.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        if "cse" in path.relative_to(UTILS_DIR).parts:
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
        pytest.skip(f"allowlisted: {ALLOWLIST[basename]}")

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
    for name in ("image_utils", "validation", "metadata", "cell_pairs"):
        candidates = [p for p in CANDIDATES if p.stem == name]
        assert candidates, f"expected to find bin/utils/{name}.py among candidates"
        assert _is_imported_anywhere(candidates[0], haystack), (
            f"matcher false-negatived a module known to be alive: {name}"
        )
