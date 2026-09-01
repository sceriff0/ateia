"""Make the claims in `docs/figures/*.html` checkable against the pipeline.

Those figures are the supplementary methods explainers -- the artefact a
reviewer reads instead of the source. Until this file existed **nothing parsed
them**, and every drift found while auditing them existed for exactly that
reason: a parameter default that had been changed for a documented reason, a
process renamed, an archived process still drawn as if it ran. All of it looked
fine, because a static HTML page fails no test.

Three claim classes are checked, chosen because a test can actually settle them:

1. **Parameter names.** Every `--<name>` and `params.<name>` a figure writes
   must be declared in `nextflow.config`'s `params {}` block.
2. **Pipeline names.** Every ``UPPER_SNAKE_CASE`` token must resolve to a
   process, a named workflow/subworkflow, or an aliased include.
3. **Parameter defaults.** Every ``<td class="k">name</td><td class="v">value</td>``
   row whose key is a declared parameter must state that parameter's actual
   `nextflow.config` default.

What this does **not** cover, and cannot:

* **Prose.** "VALIS matches with SuperGlue", "the micro pass runs twice" -- a
  claim about behaviour, not about a name or a number. Those are what the
  allow-listed figure below is stale on, and no parser settles them.
* **Numbers that are not parameter defaults.** Measured memory figures, file
  sizes, timings, tier tables written as free text rather than as a `k`/`v`
  row. They are checked by nothing here.
* **Whether a figure is COMPLETE.** A figure that omits a whole stage passes.
* **Diagram topology.** The arrows are SVG; that a name exists says nothing
  about whether it is drawn in the right place.

The floors in each test are what stop the checks becoming vacuous if the
markup is restyled: a restyle that silently stopped matching would otherwise
turn every assertion below into a loop over an empty list, which is the exact
"a count of zero passes" failure `tests/conftest.py` exists to prevent.
"""

from __future__ import annotations

import html
import importlib.util
import re
import sys
from pathlib import Path

from tests.nfmodel import REPO_ROOT, include_aliases, processes, workflows

FIGURES_DIR = REPO_ROOT / "docs" / "figures"

# --------------------------------------------------------------------------
# The shrink-only debt allowlist.
#
# Same shape as tests/test_nfmodel.py's UNREPOINTED_NF_SOURCE_READERS: an
# entry is a debt, not a permission. `test_the_allowlist_only_shrinks` below
# fails if an allow-listed figure stops failing -- so an entry cannot outlive
# the drift it excuses, and the list can only ever be edited downwards.
#
# Do NOT add a figure here to make it pass. A failing figure is a real drift
# and the figure is what gets fixed.
# --------------------------------------------------------------------------
STALE_FIGURES = {
    "registration-schematic.html": (
        "deliberately stale: its rewrite is owned by a different spec's Phase 6. "
        "It still describes the deleted COARSE front-end and carries two values "
        "for coarse_max_dim. The dangerous numbers in it were fixed. "
        "(The front-end is not named here: tests/test_no_legacy_frontends.py "
        "forbids that token across the tracked tree and this file is not on its "
        "allowlist -- which is itself a second, independent record of why this "
        "figure is exempt.)"
    ),
}

# `--<token>` forms that are NOT pipeline parameters. Each is another tool's
# flag, quoted in a figure because the pipeline renders it. Kept honest by
# `test_non_param_flags_are_really_not_params`: an entry must not name a real
# parameter (so it can never shadow one) and must still appear in a figure
# (so it cannot rot into a blanket).
NON_PARAM_FLAGS = {
    "gres": "SLURM's own flag, rendered by conf/modules.config as --gres=gpu:$gpu_type",
    "gpus": "Docker's flag, added to the run command when seg_gpu is true",
    "nv": "Singularity's flag, the --gpus equivalent",
    "method": "bin/warp_seg_qc.py's CLI flag, not a pipeline parameter",
    "tolerance": "bin/seg_qc_geojson.py's CLI flag, not a pipeline parameter",
}

# ``UPPER_SNAKE_CASE`` tokens that name something real but are not Nextflow
# process or workflow names. Kept honest the same way, by
# `test_non_pipeline_names_are_really_not_pipeline_names`.
NON_PIPELINE_NAMES = {
    "DEEPCELL_ACCESS_TOKEN": "an environment variable read by the DeepCell backend",
    "KNOWN_ARTIFACT_KINDS": "a lib/ParamUtils.groovy constant",
    "STEP_ORDER": "a lib/ParamUtils.groovy constant",
    "UNIVERSAL_QC_KINDS": "a lib/ParamUtils.groovy constant",
    "DEFAULT_BAND_BYTES": "a bin/utils constant",
    "RUSAGE_SELF": "Python's resource.RUSAGE_SELF",
}

# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

# CSS custom properties are spelled `--name` too. Every one of them lives in a
# <style> block, a style="" attribute or a var(--name) reference, so removing
# those three is what separates a design token from a claim about the pipeline.
_STYLE_BLOCK = re.compile(r"<style\b.*?</style>", re.S | re.I)
_STYLE_ATTR = re.compile(r"""\sstyle\s*=\s*(["']).*?\1""", re.S | re.I)
_VAR_REF = re.compile(r"var\(\s*--[A-Za-z0-9_-]+\s*\)")

# A Nextflow parameter is snake_case and never contains a hyphen -- true of all
# 92 declared in nextflow.config. So `--jvm-heap-gb`, `--checkpoint-dir` and
# `--no-fullres` (Python CLI flags, which this repo spells with hyphens) are
# excluded structurally rather than by name.
_FLAG = re.compile(r"--([a-z][a-z0-9_]*)(?![A-Za-z0-9_-])")
_PARAMS_DOT = re.compile(r"params\.([a-z][a-z0-9_]*)")
_UPPER = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")

_TAG = re.compile(r"<[^>]+>")
# Key and value cells of one parameter row. Neither capture may span a `</td>`,
# so a `k` cell that is not followed by a `v` cell cannot swallow the rows
# after it -- a naive `(.*?)` with re.S does exactly that, and silently merged
# eight rows into one while this was being written.
_KV_ROW = re.compile(
    r'<td class="k">((?:(?!</td>).)*)</td>\s*<td class="v[^"]*">((?:(?!</td>).)*)</td>',
    re.S,
)


def _figures():
    found = sorted(FIGURES_DIR.glob("*.html"))
    assert found, f"no figures found under {FIGURES_DIR}"
    return found


def _live_figures():
    return [f for f in _figures() if f.name not in STALE_FIGURES]


def _prose(path: Path) -> str:
    """Figure text with CSS custom properties removed. See `_STYLE_BLOCK`."""
    text = path.read_text()
    return _VAR_REF.sub(" ", _STYLE_ATTR.sub(" ", _STYLE_BLOCK.sub(" ", text)))


def _cell_text(fragment: str) -> str:
    return html.unescape(_TAG.sub("", fragment)).replace("\xa0", " ").strip()


def _config_defaults():
    """`nextflow.config`'s params {} block, via the existing reader.

    `tests/check_param_consistency.py` already owns this parse (it is the
    checker that compares the block against nextflow_schema.json). It is a
    script, not a module on the path, so it is loaded the same way
    `tests/test_param_consistency_rule.py` loads it -- rather than growing a
    second copy of a Groovy-literal parser here.
    """
    spec = importlib.util.spec_from_file_location(
        "check_param_consistency", REPO_ROOT / "tests" / "check_param_consistency.py"
    )
    module = importlib.util.module_from_spec(spec)
    # It imports `_code_view` flat. pytest happens to put tests/ on sys.path for
    # a test module in a non-package directory, so this works under a normal run
    # and raises ImportError the moment anything imports this file directly --
    # which is how a guard becomes uncollectable without failing. Explicit.
    if str(REPO_ROOT / "tests") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "tests"))
    spec.loader.exec_module(module)
    defaults = module.extract_config_defaults(
        (REPO_ROOT / "nextflow.config").read_text()
    )
    assert len(defaults) > 50, (
        f"only {len(defaults)} params parsed out of nextflow.config -- the "
        "reader has stopped working and every check below would pass vacuously"
    )
    return defaults, module.Unparsed


def _known_names():
    """Every UPPER_SNAKE_CASE name the pipeline actually defines."""
    return set(processes()) | set(workflows()) | set(include_aliases())


def _rendered(value) -> set:
    """The spellings a figure may legitimately use for a config default."""
    if value is None:
        return {"null", "none"}
    if isinstance(value, bool):
        return {"true" if value else "false"}
    if isinstance(value, list):
        inner = ", ".join(str(v) for v in value)
        return {f"[{inner}]", inner}
    if isinstance(value, float):
        out = {str(value)}
        if value.is_integer():
            out.add(str(int(value)))
        return out
    text = str(value)
    return {text, f"'{text}'", f'"{text}"'}


# --------------------------------------------------------------------------
# 1. parameter names
# --------------------------------------------------------------------------
def _flag_problems(path, defaults):
    prose = _prose(path)
    named = {m.group(1) for m in _FLAG.finditer(prose)}
    named |= {m.group(1) for m in _PARAMS_DOT.finditer(prose)}
    return [
        f"{path.name}: names `--{name}`/`params.{name}`, which nextflow.config's "
        "params {} block does not declare"
        for name in sorted(named)
        if name not in defaults and name not in NON_PARAM_FLAGS
    ]


def test_every_parameter_a_figure_names_exists():
    defaults, _ = _config_defaults()
    problems, seen = [], 0
    for path in _live_figures():
        problems += _flag_problems(path, defaults)
        seen += len(
            {m.group(1) for m in _FLAG.finditer(_prose(path))} & set(defaults)
        )
    assert seen >= 8, (
        f"only {seen} parameter mentions found across the figures -- the "
        "extractor has stopped matching and this test is checking nothing"
    )
    assert not problems, "\n".join(problems)


def test_non_param_flags_are_really_not_params():
    """Each NON_PARAM_FLAGS entry must name something that is NOT a parameter
    (an exemption must never be able to hide a real one) and must still appear
    in a figure this file checks. An entry that stops matching is exempting
    nothing and is deleted -- the shrink-only rule, applied to this list."""
    defaults, _ = _config_defaults()
    prose = " ".join(_prose(p) for p in _live_figures())
    used = {m.group(1) for m in _FLAG.finditer(prose)}
    problems = []
    for name in NON_PARAM_FLAGS:
        if name in defaults:
            problems.append(
                f"--{name} IS a declared parameter now -- remove it from "
                "NON_PARAM_FLAGS, it is exempting a real one"
            )
        if name not in used:
            problems.append(
                f"--{name} no longer appears in any checked figure -- remove it "
                "from NON_PARAM_FLAGS"
            )
    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------------
# 2. pipeline names
# --------------------------------------------------------------------------
def _name_problems(path, known):
    named = {m.group(0) for m in _UPPER.finditer(path.read_text())}
    return [
        f"{path.name}: names `{name}`, which is not a process, a workflow or an "
        "aliased include anywhere in the pipeline"
        for name in sorted(named)
        if name not in known and name not in NON_PIPELINE_NAMES
    ]


def test_every_pipeline_name_a_figure_uses_exists():
    """A figure naming a process that was renamed or archived is the drift this
    catches. Resolution is against processes + workflows + include aliases,
    because a subworkflow and an aliased include are pipeline names too -- and a
    resolver that saw only `processes()` would report SEG_QC and SEG_QC_SEGMENT
    as typos, which is how an allowlist starts absorbing real names."""
    known = _known_names()
    problems, seen = [], set()
    for path in _live_figures():
        problems += _name_problems(path, known)
        seen |= {m.group(0) for m in _UPPER.finditer(path.read_text())} & known
    assert len(seen) >= 20, (
        f"only {len(seen)} known pipeline names found across the figures -- the "
        "extractor has stopped matching"
    )
    assert not problems, "\n".join(problems)


def test_non_pipeline_names_are_really_not_pipeline_names():
    """The NON_PIPELINE_NAMES counterpart of the flag check above."""
    known = _known_names()
    used = set()
    for path in _live_figures():
        used |= {m.group(0) for m in _UPPER.finditer(path.read_text())}
    problems = []
    for name in NON_PIPELINE_NAMES:
        if name in known:
            problems.append(
                f"{name} IS a pipeline name now -- remove it from "
                "NON_PIPELINE_NAMES, it is exempting a real one"
            )
        if name not in used:
            problems.append(
                f"{name} no longer appears in any checked figure -- remove it "
                "from NON_PIPELINE_NAMES"
            )
    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------------
# 3. parameter defaults
# --------------------------------------------------------------------------
def _default_rows(path, defaults):
    """`(param, figure_value)` for every k/v row whose key is a declared param.

    A row whose key is not a bare identifier is skipped on purpose: the figures
    use the same table markup for compound keys (`seg_n_tiles_y / _x`) and for
    rows that are not parameters at all (`step 1 · features`). Those carry no
    single default to compare against, and check 1 above is what catches a
    misspelled parameter NAME.
    """
    for m in _KV_ROW.finditer(path.read_text()):
        key = _cell_text(m.group(1))
        if re.fullmatch(r"[a-z][a-z0-9_]*", key) and key in defaults:
            yield key, _cell_text(m.group(2))


def _value_problems(path, defaults, unparsed_type):
    problems = []
    for key, shown in _default_rows(path, defaults):
        expected = defaults[key]
        if isinstance(expected, unparsed_type):
            continue  # a Groovy expression the config reader cannot evaluate
        # `…`/`...` is the figures' explicit "truncated for width" marker.
        if shown.endswith(("…", "...")):
            stem = shown.rstrip(".…").strip()
            if stem and any(s.startswith(stem) for s in _rendered(expected)):
                continue
        if shown not in _rendered(expected):
            problems.append(
                f"{path.name}: states `{key} = {shown}` but nextflow.config "
                f"declares {expected!r}"
            )
    return problems


def test_every_default_a_figure_states_matches_nextflow_config():
    """The drift with teeth: a figure advertising a default that was changed.

    `nextflow.config` is the single source of truth for defaults -- a second
    declaration anywhere in the pipeline is already forbidden
    (`tests/test_no_duplicate_param_defaults.py`). A figure is a second
    declaration too; it just cannot be executed, so nothing noticed.
    """
    defaults, unparsed = _config_defaults()
    problems, rows = [], 0
    for path in _live_figures():
        rows += len(list(_default_rows(path, defaults)))
        problems += _value_problems(path, defaults, unparsed)
    assert rows >= 60, (
        f"only {rows} parameter-default rows extracted from the figures -- the "
        "table markup has changed and this test would pass on an empty loop"
    )
    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------------
# the allowlist meta-test
# --------------------------------------------------------------------------
def test_the_allowlist_only_shrinks():
    """A STALE_FIGURES entry must name a figure that exists and that still
    fails at least one check above.

    An entry that has been fixed but not removed is the same rot
    `tests/test_nfmodel.py` guards against: the exemption outlives the drift,
    and the next real regression in that file lands silently. Removing the
    entry is then a one-line diff, and this failure says so.
    """
    defaults, unparsed = _config_defaults()
    known = _known_names()
    stale = []
    for name in STALE_FIGURES:
        path = FIGURES_DIR / name
        if not path.is_file():
            stale.append(f"{name}: no longer exists -- remove it from STALE_FIGURES")
            continue
        problems = (
            _flag_problems(path, defaults)
            + _name_problems(path, known)
            + _value_problems(path, defaults, unparsed)
        )
        if not problems:
            stale.append(
                f"{name}: now passes every check -- remove it from STALE_FIGURES "
                "so the next drift in it is caught"
            )
    assert not stale, "\n".join(stale)


def test_the_allowlist_is_not_a_blanket():
    """At least one figure must be checked. An allowlist that grew to cover
    every figure would leave all five tests above iterating over nothing."""
    assert len(_live_figures()) >= len(_figures()) - 1
