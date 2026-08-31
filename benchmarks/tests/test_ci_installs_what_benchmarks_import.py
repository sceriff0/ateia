"""Every third-party module benchmarks/ imports must be installed by benchmarks.yml.

WHY THIS EXISTS. On 2026-08-26 and 2026-08-27 the stare-bench job failed in ~25 s with
`Interrupted: 4 errors during collection` and exit code 2. Nothing was wrong with any
test: benchmarks/analysis/lib/regress.py imports sklearn and test_plotting.py imports
matplotlib, and the job's `pip install` line named neither. pytest died during COLLECTION,
so ZERO tests ran -- and the run looked like an ordinary red build rather than a job that
never started. It stayed that way for five days.

This is the CI-side twin of tests/test_fixtures_have_a_producer.py: a test can only run
where the thing it needs exists. That guard covers fixtures; this one covers imports.

TWO LEGS, scanned differently, because they fail differently.

  benchmarks/ itself -- TOP-LEVEL imports only. An import inside a function is a
  deliberate optional dependency (bin/utils/coarse_align.py's torch/kornia block is the
  pattern) and is allowed to be absent; a top-level one kills collection.

  The SUBPROCESS leg -- bin/tiled_{coarse,reg_tile,solve}.py, which run_unit.py shells out
  to, plus everything they import transitively inside bin/. Here EVERY import level counts,
  because those scripts are EXECUTED: bin/utils/tiled_io.py imports zarr inside a function,
  which no top-level scan can see, and it surfaced only as `tiled_coarse.py failed (1): No
  module named 'zarr'` at runtime -- after collection succeeded, so the previous version of
  this guard passed while CI still failed. The pipeline's own dependencies are part of this
  job's contract precisely because the unit rung runs the pipeline's real entry points.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "benchmarks.yml"
PKG = REPO / "benchmarks"

# import name -> the distribution that provides it, where they differ.
DIST_OF = {
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "yaml": "pyyaml",
    "cv2": "opencv-python-headless",
    "PIL": "pillow",
}

# Modules that are NOT third-party: stdlib, and the repo's own top-level packages.
_FIRST_PARTY_PKGS = {"benchmarks", "bin", "lib", "tests", "utils"}


def _is_first_party(mod: str) -> bool:
    """True for the repo's own packages AND for anything importable out of bin/utils.

    The second half is not a special case. benchmarks/ashlar/solve.py and
    benchmarks/stare_bench/run_unit.py both sys.path-insert the pipeline's bin/utils and
    then `from tiled_manifest import ...` -- reusing the pipeline's real manifest code so
    ashlar's manifest cannot drift from STARE's. Resolving those against the filesystem,
    rather than listing them, means a new shared module does not have to be remembered
    here to avoid being reported as a missing PyPI distribution that does not exist.
    """
    if mod in _FIRST_PARTY_PKGS:
        return True
    for d in (REPO / "bin", REPO / "bin" / "utils"):
        if (d / f"{mod}.py").exists() or (d / mod / "__init__.py").exists():
            return True
    return False


def _installed_distributions() -> set[str]:
    """Distribution names benchmarks.yml pip-installs, minus version pins and flags."""
    # Join backslash continuations FIRST. A multi-line `pip install a \\\n b` has most of
    # its packages on lines that do not themselves start with "pip install"; scanning
    # line-by-line silently drops them, and this guard would then pass while checking a
    # fraction of the install. test_the_install_parser_actually_finds_packages caught
    # exactly that bug in this function's first draft.
    text = re.sub(r"\\\s*\n\s*", " ", WORKFLOW.read_text())
    names: set[str] = set()
    for line in text.splitlines():
        line = line.strip().lstrip("|").strip()
        if not line.startswith("pip install"):
            continue
        for tok in line[len("pip install"):].split():
            if tok.startswith("-"):          # --index-url consumes the rest of the command
                break
            names.add(re.split(r"[=<>!~\[]", tok)[0].lower())
    return names


def _top_level_imports() -> dict[str, set[Path]]:
    """module -> files that import it at MODULE level (not inside a function/class)."""
    found: dict[str, set[Path]] = {}
    for py in PKG.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text())
        except SyntaxError:                   # not this guard's job to police
            continue
        for node in tree.body:                # tree.body == top level only
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:                # relative import -> first-party
                    continue
                mods = [(node.module or "").split(".")[0]]
            else:
                continue
            for m in mods:
                if m and not _is_first_party(m) and m not in sys.stdlib_module_names:
                    found.setdefault(m, set()).add(py.relative_to(REPO))
    return found


def _bin_module_path(name: str) -> Path | None:
    for d in (REPO / "bin", REPO / "bin" / "utils"):
        cand = d / f"{name}.py"
        if cand.exists():
            return cand
    return None


def _subprocess_leg_imports() -> dict[str, set[str]]:
    """Third-party modules reachable from the bin/ scripts the harness executes.

    Transitive over bin/ first-party modules, and at EVERY import level -- see the module
    docstring for why a top-level-only scan is not enough here.
    """
    entry = sorted(
        set(re.findall(r'"(tiled_\w+\.py)"', (PKG / "stare_bench" / "run_unit.py").read_text()))
    )
    assert entry, "found no bin/*.py subprocess entry points in run_unit.py"
    found: dict[str, set[str]] = {}
    seen: set[str] = set()

    def walk(mod: str) -> None:
        if mod in seen:
            return
        seen.add(mod)
        path = _bin_module_path(mod)
        if path is None:
            return
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                mods = [(node.module or "").split(".")[0]]
            else:
                continue
            for m in mods:
                if not m or m in sys.stdlib_module_names:
                    continue
                if _is_first_party(m):
                    walk(m)
                else:
                    found.setdefault(m, set()).add(path.name)

    for e in entry:
        walk(e[:-3])
    return found


def test_benchmarks_yml_installs_what_the_subprocess_leg_needs():
    """run_unit.py executes the pipeline's real bin/tiled_*.py, so their deps are ours."""
    installed = _installed_distributions()
    missing = []
    for mod, files in sorted(_subprocess_leg_imports().items()):
        dist = DIST_OF.get(mod, mod).lower()
        if dist not in installed:
            missing.append(f"{mod} (dist {dist}) -- imported by {', '.join(sorted(files))}")
    assert not missing, (
        "benchmarks.yml does not install module(s) the subprocess leg needs:\n  "
        + "\n  ".join(missing)
        + "\nThese surface at RUNTIME as `<script>.py failed (1): No module named ...`, "
          "AFTER collection succeeds, so the collection-time guard above cannot catch them."
    )


def test_the_subprocess_scanner_reaches_through_bin_utils():
    """Vacuity check: the walk must actually leave the entry scripts.

    zarr is the canary -- it lives in bin/utils/tiled_io.py, two hops from tiled_coarse.py
    and inside a function. If the scanner ever stops finding it, the guard above has
    silently narrowed to the entry scripts' own top-level imports.
    """
    found = _subprocess_leg_imports()
    assert "zarr" in found, sorted(found)
    assert "torch" in found, sorted(found)


def test_benchmarks_yml_installs_every_module_benchmarks_imports():
    installed = _installed_distributions()
    missing = []
    for mod, files in sorted(_top_level_imports().items()):
        dist = DIST_OF.get(mod, mod).lower()
        if dist not in installed:
            where = ", ".join(sorted(str(f) for f in files)[:3])
            missing.append(f"{mod} (dist {dist}) -- imported by {where}")
    assert not missing, (
        "benchmarks.yml does not install module(s) benchmarks/ imports at top level:\n  "
        + "\n  ".join(missing)
        + "\nWithout them pytest dies during COLLECTION (exit 2, zero tests run), which "
          "reads as an ordinary red build. Add them to the job's pip install line, pinned "
          "to the version ci.yml uses, or move the import inside the function that needs it."
    )


def test_the_scanner_actually_finds_imports():
    """A scanner that silently returns nothing would make the guard above vacuous."""
    mods = _top_level_imports()
    assert len(mods) >= 5, sorted(mods)
    assert "numpy" in mods, sorted(mods)


def test_the_install_parser_actually_finds_packages():
    """Likewise for the workflow side."""
    installed = _installed_distributions()
    assert {"numpy", "pytest", "torch"} <= installed, sorted(installed)
