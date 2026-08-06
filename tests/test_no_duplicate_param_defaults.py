#!/usr/bin/env python3
"""Guard against duplicated parameter defaults.

`nextflow.config`'s `params {}` block is the single source of truth for
parameter defaults. Process scripts and `conf/modules.config` must not
re-declare a *concrete* fallback via Groovy's `?:` operator, because `?:` is
falsy-coalescing (not null-coalescing): `params.tilex ?: 256` silently
rewrites a legitimate `--tilex 0` override to `256`, and if the fallback
literal ever drifts from `nextflow.config` the module lies about the real
default.

`params.x ?: <literal>` is only legitimate where `nextflow.config` declares
`x = null` — there, `?:` is the live "null means derive a value at runtime"
contract (e.g. `preproc_pool_workers`, which falls back to `task.cpus`).

This test derives its allowlist directly from `nextflow.config`'s `params {}`
block, so it keeps working whether a param is null-declared today or someone
flips it to a concrete value tomorrow (or vice versa) -- no hand-curated list
to fall out of sync.

A second, independent check below closes the same blind spot on the Python
side: `bin/**/*.py` argparse defaults are just as capable of drifting from
`nextflow.config` as a Groovy `?:` fallback, and the pipeline never notices
because it always passes the flag explicitly -- only hand-invocation of the
script sees the stale default. That check is name-based (a flag normalizes
to a params key or it's out of scope -- no fuzzy/value matching) and parses
Python with `ast`, not regex, so a non-literal `default=` (a variable, a
call, an f-string) can be detected and skipped rather than mis-compared.
"""

from __future__ import annotations

import ast
import re
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "nextflow.config"
BIN_DIR = ROOT / "bin"

# `params.<name> ?:` -- the pattern under audit.
FALLBACK_RE = re.compile(r"params\.([A-Za-z_][A-Za-z0-9_]*)\s*\?:")

# Files scanned for `params.x ?: <literal>` fallbacks.
#
# `lib/*.groovy` is in the list because parameter reads move there: SegBackends holds
# the per-backend defaults SEGMENT's script block used to inline, including
# `params.instanseg_model_dir ?: "$PWD/.instanseg_cache"`. A fallback that escaped the
# scan by being one directory over would be exactly the drift this test exists to catch.
SCANNED_GLOBS = [
    "main.nf",
    "modules/local/*.nf",
    "conf/*.config",
    "workflows/*.nf",
    "subworkflows/**/*.nf",
    "lib/*.groovy",
]


def _extract_params_block(config_text: str) -> str:
    """Return the raw text inside the top-level `params { ... }` block."""
    start = config_text.find("params {")
    if start == -1:
        raise ValueError("Could not locate `params { ... }` block in nextflow.config")

    brace_start = config_text.find("{", start)
    depth = 0
    end = None
    for i in range(brace_start, len(config_text)):
        ch = config_text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end is None:
        raise ValueError("Unclosed `params { ... }` block in nextflow.config")

    return config_text[brace_start + 1 : end]


def _strip_line_comment(line: str) -> str:
    """Strip a trailing `// ...` comment, ignoring `//` inside string literals."""
    in_single = False
    in_double = False
    i = 0
    while i < len(line) - 1:
        ch = line[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "/" and line[i + 1] == "/" and not in_single and not in_double:
            return line[:i]
        i += 1
    return line


def parse_declared_params(config_text: str) -> dict[str, str]:
    """Parse the `params {}` block into {name: declared_value_text}.

    `declared_value_text` is the trimmed right-hand side of the assignment
    (comments stripped), so callers can test `value == "null"`.
    """
    block = _extract_params_block(config_text)
    declared: dict[str, str] = {}
    for raw_line in block.splitlines():
        line = _strip_line_comment(raw_line).strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$", line)
        if m:
            declared[m.group(1)] = m.group(2)
    return declared


def find_fallback_sites() -> list[tuple[Path, int, str, str]]:
    """Return (file, line_no, param_name, line_text) for every `params.x ?:` site."""
    sites: list[tuple[Path, int, str, str]] = []
    for pattern in SCANNED_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            text = path.read_text()
            for line_no, line in enumerate(text.splitlines(), start=1):
                for m in FALLBACK_RE.finditer(line):
                    sites.append((path, line_no, m.group(1), line.strip()))
    return sites


def test_no_duplicate_param_defaults():
    """Every `params.x ?: <literal>` site must name a param declared `null`.

    Where `nextflow.config` gives `x` a concrete default, any `?:` fallback
    beside `params.x` is unreachable duplication (or worse: a stale literal
    that disagrees with the real default) and must be deleted, leaving the
    bare `params.x`.
    """
    config_text = CONFIG_PATH.read_text()
    declared = parse_declared_params(config_text)
    sites = find_fallback_sites()

    assert sites, "No `params.x ?:` sites found -- scan patterns may be stale."

    offending = []
    for path, line_no, name, line_text in sites:
        if name not in declared:
            offending.append(
                f"{path.relative_to(ROOT)}:{line_no}: params.{name} ?: ... "
                f"(param not found in nextflow.config params{{}} block at all)"
            )
            continue
        if declared[name] != "null":
            offending.append(
                f"{path.relative_to(ROOT)}:{line_no}: params.{name} ?: ... "
                f"duplicates nextflow.config's concrete default "
                f"`{name} = {declared[name]}` -- delete the `?: ...` fallback."
            )

    assert not offending, (
        f"{len(offending)} site(s) duplicate a concrete nextflow.config default "
        "via `?:`. Delete the fallback literal, leaving the bare `params.x`, "
        "unless nextflow.config declares the param `null` (in which case `?:` "
        "is the live null-means-derive contract):\n" + "\n".join(offending)
    )


# ---------------------------------------------------------------------------
# bin/**/*.py argparse defaults vs. nextflow.config
# ---------------------------------------------------------------------------
#
# The `?:` check above only sees Groovy. A `bin/foo.py` argparse `default=`
# is just as capable of drifting from `nextflow.config` -- the pipeline
# always passes the flag explicitly (ext.args / a process script), so a
# stale Python default only bites hand-invocation of the script.
#
# The authority for "which param does this flag mean" is the pipeline's own
# invocation text, not a guess from the flag's spelling: `build_flag_param_map`
# scans `modules/local/*.nf` + `conf/modules.config` for the two shapes the
# pipeline actually uses (`--flag ${params.key}` directly, or one level of
# `def var = params.key` aliasing) and derives a flag -> param map from that.
# A flag whose name happens to equal a params key (e.g. `--pyramid-resolutions`
# / `pyramid_resolutions`) is *also* covered by that direct form, so the
# straight name-equality check that shipped before this exists purely as a
# fallback for flags the derived map doesn't find -- it's still correct when
# it fires, just no longer the primary authority. A flag like
# `segment_to_geojson.py`'s `--tolerance` matches no param named `tolerance`
# via either path and stays out of scope by design.

# Deliberate divergences between a bin/*.py argparse default and the
# nextflow.config default its flag resolves to (via the derived map or the
# name-equality fallback). Keyed by "<file>:<flag>". Every entry here is
# either a documented, intentional standalone-use fallback (verified against
# the script's own help text/docstring) or a real divergence in a file this
# task is not authorized to edit (noted as such).
ARGPARSE_DEFAULT_ALLOWLIST = {
    "extract_cell_properties.py:--outdir": {
        "reason": (
            "Standalone-CLI convenience: defaults to the cwd so the script "
            "is hand-runnable without an explicit --outdir. nextflow.config's "
            "`outdir` is null (required; resolved to the run's real output "
            "directory by the pipeline) -- a different contract than 'default "
            "location for a manual invocation'."
        ),
    },
    "quantify.py:--outdir": {
        "reason": (
            "Same standalone-CLI convenience as extract_cell_properties.py's "
            "--outdir: defaults to the cwd for hand-invocation; the pipeline "
            "always passes an explicit --outdir."
        ),
    },
    "segment_instantseg.py:--pixel-size": {
        "reason": (
            "default=None is a real fallback, not a stale literal: the "
            "script's own help text says 'Override pixel size (um/px). If "
            "omitted, InstanSeg auto-detects from OME metadata.' Hardcoding "
            "nextflow.config's 0.325 here would remove that auto-detect path "
            "for standalone use."
        ),
    },
    "segment_to_geojson.py:--nuclear-markers": {
        "reason": (
            "default=None is intentional per the script's own help text: "
            "'SEG_QC_GEOJSON always passes params.nuclear_markers; the "
            "default is only for standalone use.' The pipeline never relies "
            "on this default."
        ),
    },
    "split_multichannel.py:--nuclear-markers": {
        "reason": (
            "Same pattern as segment_to_geojson.py: 'SPLIT_CHANNELS always "
            "passes params.nuclear_markers; the default is only for "
            "standalone use.'"
        ),
    },
    "segment_cellsam.py:--block-size": {
        "reason": (
            "Real divergence surfaced by the derived flag->param map "
            "(conf/modules.config:383 `--block-size ${params.seg_cellsam_"
            "block_size}` resolves --block-size unambiguously): argparse "
            "default=400 vs. nextflow.config's seg_cellsam_block_size=1024. "
            "bin/segment_cellsam.py is not one of the files this task (Task "
            "1 of the arch-candidates-1-8 refactor) is authorized to edit, "
            "so it is allowlisted here rather than fixed. Flagged in the "
            "task report for a follow-up task to actually fix the default."
        ),
    },
}


def _config_value_to_python(text: str):
    """Translate a `nextflow.config` params{} RHS into a comparable Python value.

    Groovy's `true`/`false`/`null` aren't Python literals; quoted strings,
    numbers, and list literals of those already parse as valid Python via
    `ast.literal_eval` (Groovy's `['a', 'b']` is also a valid Python list
    literal).
    """
    keyword_literals = {"true": True, "false": False, "null": None}
    if text in keyword_literals:
        return keyword_literals[text]
    return ast.literal_eval(text)


def _normalize_flag(flag: str) -> str:
    """`--foo-bar` / `--foo_bar` (or a short `-x`) -> `foo_bar` / `x`."""
    return flag.lstrip("-").replace("-", "_")


# A CLI flag immediately followed by one-or-more consecutive `${...}`
# interpolations, e.g. `--n_iter ${params.preproc_n_iter}` or
# `--n-tiles ${params.seg_n_tiles_y} ${params.seg_n_tiles_x}` (two
# interpolations -- a composite flag this check deliberately does not
# resolve; see `build_flag_param_map`).
_FLAG_INTERP_RE = re.compile(r"--([A-Za-z0-9][A-Za-z0-9_-]*)((?:\s+\$\{[^}]*\})+)")
# A single `${...}` interpolation's content, only when it is one bare token
# (a dotted `params.key` reference or a plain variable name) with no
# surrounding whitespace -- i.e. not `${params.x ?: task.cpus}` or
# `${row.ix}`-via-method-call style expressions, which are one level deeper
# than this check resolves.
_INTERP_ATOM_RE = re.compile(r"\$\{\s*([^}\s]+)\s*\}")
# `def <var> = params.<key>` -- the one level of aliasing this check resolves.
_DEF_ALIAS_RE = re.compile(
    r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*params\.([A-Za-z_][A-Za-z0-9_]*)\s*$",
    re.MULTILINE,
)


def build_flag_param_map() -> dict[str, str]:
    """Derive an authoritative flag -> nextflow.config param-key map from how
    the pipeline itself invokes bin/*.py scripts, instead of guessing a
    correspondence from the flag's spelling.

    Scans `modules/local/*.nf` and `conf/modules.config` for exactly two
    shapes (this is deliberately NOT a general expression evaluator -- it
    resolves at most one level):

      - direct:      `--flag ${params.key}`
      - def-aliased: `def var = params.key`  ...  `--flag ${var}`

    A flag is excluded from the returned map (left for the name-equality
    fallback, or left out of scope entirely) when:

      - it resolves to more than one *distinct* param key across the
        scanned files -- e.g. `--scale-factor` means `qc_scale_factor` in
        `generate_registration_qc.nf` but `preprocess_qc_scale_factor` in
        `generate_preprocess_qc.nf`: two different scripts, two different
        params, coincidentally the same flag name;
      - any occurrence of the flag is followed by more than one
        interpolation -- a composite/nargs-style flag consuming multiple
        params at once, e.g. `--n-tiles ${params.seg_n_tiles_y}
        ${params.seg_n_tiles_x}` (comparing a 2-element argparse default
        against one scalar param would be a category error, not a real
        drift check); or
      - the interpolated expression isn't a bare `params.key` or a bare
        aliased variable -- e.g. `${params.preproc_pool_workers ?:
        task.cpus}` is a fallback expression one level deeper than this
        resolves, so `--n_workers` is intentionally left unmapped rather
        than compared against `preproc_pool_workers`'s null default (which
        would be a false positive: null there means "derive at runtime",
        the exact contract `test_no_duplicate_param_defaults` already
        recognizes for the Groovy `?:` check above).
    """
    nf_files = sorted(ROOT.glob("modules/local/*.nf")) + [ROOT / "conf/modules.config"]

    raw: dict[str, set[str]] = {}
    multivalue: set[str] = set()

    for path in nf_files:
        text = path.read_text()
        aliases = dict(_DEF_ALIAS_RE.findall(text))
        for m in _FLAG_INTERP_RE.finditer(text):
            flag = _normalize_flag(m.group(1))
            atoms = _INTERP_ATOM_RE.findall(m.group(2))
            if len(atoms) != 1:
                multivalue.add(flag)
                continue
            atom = atoms[0]
            key = None
            if atom.startswith("params."):
                key = atom[len("params.") :].split(".")[0]
            elif atom in aliases:
                key = aliases[atom]
            if key is not None:
                raw.setdefault(flag, set()).add(key)

    return {
        flag: next(iter(keys))
        for flag, keys in raw.items()
        if len(keys) == 1 and flag not in multivalue
    }


def _iter_add_argument_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            yield node


def find_argparse_default_sites():
    """Scan `bin/**/*.py` for `add_argument()` calls whose flag resolves to a
    `nextflow.config` params{} key, via the derived flag->param map or (as a
    fallback) exact name-equality.

    Returns `(matched, skipped, declared)`:
      - `matched`: `(path, flag, param_name, python_default, via)` for calls
        with a literal `default=` (or no `default=` kwarg at all, which
        argparse itself treats as `default=None`). `via` is `"map"` when the
        derived flag->param map resolved the flag, `"name-eq"` when only the
        name-equality fallback did.
      - `skipped`: `(path, flag, param_name, via)` for calls whose
        `default=` is a non-literal expression (a variable, a call, an
        f-string, ...) that `ast.literal_eval` cannot statically evaluate.
      - `declared`: the `nextflow.config` params{} block, as parsed by
        `parse_declared_params`.
    """
    declared = parse_declared_params(CONFIG_PATH.read_text())
    flag_param_map = build_flag_param_map()

    matched: list[tuple[Path, str, str, object, str]] = []
    skipped: list[tuple[Path, str, str, str]] = []
    for path in sorted(BIN_DIR.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for call in _iter_add_argument_calls(tree):
            flags = [
                arg.value
                for arg in call.args
                if isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and arg.value.startswith("-")
            ]
            default_kw = next(
                (kw.value for kw in call.keywords if kw.arg == "default"), None
            )
            for flag in flags:
                name = _normalize_flag(flag)
                if name in flag_param_map:
                    key, via = flag_param_map[name], "map"
                elif name in declared:
                    key, via = name, "name-eq"
                else:
                    continue  # out of scope: no correspondence found at all
                if default_kw is None:
                    matched.append((path, flag, key, None, via))
                    continue
                try:
                    python_default = ast.literal_eval(default_kw)
                except (ValueError, TypeError):
                    skipped.append((path, flag, key, via))
                    continue
                matched.append((path, flag, key, python_default, via))
    return matched, skipped, declared


def test_no_duplicate_bin_argparse_defaults():
    """Every `bin/**/*.py` argparse default that resolves to a
    `nextflow.config` param (via the derived flag->param map, or by exact
    name-equality as a fallback) must equal that param's default.

    Non-literal defaults (a variable, a call, an f-string) can't be
    statically compared and are skipped -- see the warning this test always
    emits reporting how many flags were checked via the derived map, how
    many via the name-equality fallback, and how many were skipped, so none
    of those counts silently disappear.
    """
    matched, skipped, declared = find_argparse_default_sites()

    assert matched or skipped, (
        "No bin/**/*.py argparse flag resolved to a nextflow.config params "
        "key at all -- the scan may be broken."
    )

    offending = []
    for path, flag, key, python_default, via in matched:
        allowlist_key = f"{path.name}:{flag}"
        if allowlist_key in ARGPARSE_DEFAULT_ALLOWLIST:
            continue
        config_default = _config_value_to_python(declared[key])
        if python_default != config_default:
            offending.append(
                f"{path.relative_to(ROOT)}: {flag} default={python_default!r} "
                f"but nextflow.config's {key} = {config_default!r} (resolved "
                f"via {via}). Either fix the Python default to match, or add "
                f"an ARGPARSE_DEFAULT_ALLOWLIST entry keyed {allowlist_key!r} "
                "with a reason."
            )

    via_map = sum(1 for *_, via in matched if via == "map")
    via_name_eq = sum(1 for *_, via in matched if via == "name-eq")
    warnings.warn(
        f"bin argparse-default check: {via_map} flag(s) checked via the "
        f"derived flag->param map, {via_name_eq} via the name-equality "
        f"fallback, {len(skipped)} skipped (non-literal default=, cannot be "
        "statically compared)"
        + (
            ": " + ", ".join(f"{p.relative_to(ROOT)}:{f}" for p, f, _, _ in skipped)
            if skipped
            else ""
        ),
        stacklevel=1,
    )

    assert not offending, (
        f"{len(offending)} bin/*.py argparse default(s) drifted from "
        "nextflow.config:\n" + "\n".join(offending)
    )


def test_argparse_default_allowlist_entries_have_reasons():
    """Every `ARGPARSE_DEFAULT_ALLOWLIST` entry must be a `{reason}` dict.

    Forces a non-empty, structured reason instead of a bare string or a
    silently-forgotten entry -- mirrors the shape enforced on
    `test_no_dead_bin_modules.py`'s `ALLOWLIST`.
    """
    for key, entry in ARGPARSE_DEFAULT_ALLOWLIST.items():
        assert isinstance(entry, dict) and set(entry) == {"reason"}, (
            f"ARGPARSE_DEFAULT_ALLOWLIST[{key!r}] must be a dict with exactly "
            "a 'reason' key."
        )
        assert isinstance(entry["reason"], str) and entry["reason"].strip(), (
            f"ARGPARSE_DEFAULT_ALLOWLIST[{key!r}]['reason'] must be a "
            "non-empty string."
        )
