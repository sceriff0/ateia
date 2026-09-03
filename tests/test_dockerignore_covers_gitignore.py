"""A directory git ignores must not be uploaded to the Docker builder either.

THE BUILD CONTEXT IS THE REPOSITORY ROOT. `.github/workflows/containers.yml` passes
`context: .` because ten of the eleven Dockerfiles COPY their pins from `requirements/`
and all eleven COPY their own `containers/<dir>/smoke.sh`. A per-directory context
cannot reach outside itself, so the context had to widen -- and `.dockerignore` is the
only thing keeping the upload small.

WHY CI CANNOT TELL YOU THIS IS BROKEN. The runner's checkout is clean: every directory
this guard is about is untracked local scratch, so `docker build .` on a runner uploads
~10 MB whatever `.dockerignore` says. On a developer's checkout the same command
uploaded ~13 GB when this guard was written (`challenge/` alone was 12 GB), because
`challenge/`, `valis_lib/`, `notebooks/`, `traces/`, `.venv-bench/`, `illum_bench/`,
`legacy/`, `pixie-main/` and `site/` were gitignored but not dockerignored. A green CI
build is silent about the entire class, which is what makes a static guard the right
instrument.

SCOPE IS TOP-LEVEL DIRECTORIES ONLY, and deliberately so. A `.gitignore` rule like
`tests/output/` names a directory INSIDE a tracked one; dockerignoring its first segment
(`tests`) would be wrong. Only a rule that is exactly `name`, `name/` or `name/*`, whose
`name` exists as a top-level directory, is treated as a claim this guard can check.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GITIGNORE = REPO / ".gitignore"
DOCKERIGNORE = REPO / ".dockerignore"

# `name`, `/name`, `name/`, `/name/`, `name/*`, `/name/*` -- and nothing with a second
# path segment or a glob in the NAME itself.
_TOP_LEVEL_DIR_RULE = re.compile(r"^/?([A-Za-z0-9._-]+)(?:/\*?)?$")


def _rules(path):
    """Non-empty, non-comment, non-negated lines of an ignore file."""
    out = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("!"):
            out.append(line)
    return out


def gitignored_top_level_dirs():
    """Top-level directories .gitignore claims, that actually exist on disk."""
    found = set()
    for line in _rules(GITIGNORE):
        match = _TOP_LEVEL_DIR_RULE.match(line)
        if match and (REPO / match.group(1)).is_dir():
            found.add(match.group(1))
    return found


def _gitignore_top_level_rule_names():
    """Bare names of every top-level-shaped `.gitignore` rule, WITHOUT filtering by
    on-disk existence.

    Used only to prove the parser itself still finds something. gitignored_top_level_dirs()
    deliberately filters by is_dir() -- that is correct for the coverage assertion below,
    which must not demand .dockerignore list a directory nobody has -- but it also means
    that function legitimately returns an EMPTY set on any clean checkout (this repo's own
    fresh worktrees, and CI, materialise none of these scratch directories). A non-vacuity
    check built on is_dir() would then fail on every clean checkout, which is exactly the
    condition this guard exists to protect even when nobody has hit it yet. Counting rules
    instead of on-disk directories keeps the check meaningful there too.
    """
    found = set()
    for line in _rules(GITIGNORE):
        match = _TOP_LEVEL_DIR_RULE.match(line)
        if match:
            found.add(match.group(1))
    return found


def dockerignored_names():
    """The bare names .dockerignore excludes, normalised the same way."""
    found = set()
    for line in _rules(DOCKERIGNORE):
        match = _TOP_LEVEL_DIR_RULE.match(line)
        if match:
            found.add(match.group(1))
    return found


def test_the_scan_is_not_vacuous():
    """A guard whose "good" answer is an absence must prove it looked at something.

    Both halves have a failure mode that reports success: an unreadable .gitignore
    yields an empty claim set and every assertion below then passes having compared
    nothing. This deliberately does NOT assert on-disk existence -- checking that a
    gitignored scratch directory currently exists on disk fails on every clean checkout
    and in CI, where none of them have been materialised, which is not evidence the
    parser is stale. It counts RULES instead: how many top-level-shaped entries
    .gitignore's text claims, independent of whether any of them exist right now.
    Measured 2026-09-03 in this checkout: 54.
    """
    claimed = _gitignore_top_level_rule_names()
    assert len(claimed) >= 40, (
        f".gitignore parsed to only {len(claimed)} top-level-shaped rules. The rule "
        "pattern, not the repository, is what changed -- this repo's .gitignore names "
        "well over forty of them."
    )
    assert len(dockerignored_names()) >= 8, (
        ".dockerignore parsed to fewer than eight entries; the parser is stale."
    )


def test_every_gitignored_top_level_directory_is_also_dockerignored():
    missing = sorted(gitignored_top_level_dirs() - dockerignored_names())
    assert not missing, (
        f"these top-level directories are gitignored but NOT dockerignored: {missing}. "
        "The build context is the repository root, so `docker build .` from a developer "
        "checkout uploads every one of them to the builder. CI cannot catch this -- its "
        "checkout is clean. Add each name to .dockerignore."
    )


def test_the_root_html_reviews_are_excluded():
    """The gitignored root-level *.html audit reports are files, not directories, so the
    directory rule above cannot see them. They are excluded by one glob."""
    assert "/*.html" in _rules(DOCKERIGNORE), (
        ".dockerignore must carry `/*.html`. The architecture-review HTML files at the "
        "repository root are gitignored one by one (so a future INTENDED root HTML is "
        "not swallowed silently), which no directory rule covers."
    )


def test_environment_yml_is_gone():
    """It was a conda file nothing referenced and pinning nothing (`python=3.9` plus bare
    package names, including `aicsimageio`, which tests/test_container_harmonisation.py's
    FORBIDDEN tuple bans from every image)."""
    assert not (REPO / "environment.yml").exists(), (
        "environment.yml is back. Nothing references it, it pins no version, and it "
        "installs aicsimageio, which is on test_container_harmonisation.FORBIDDEN. The "
        "authoritative environments are the eleven images under containers/."
    )
