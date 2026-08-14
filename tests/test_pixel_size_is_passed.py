"""Every process whose script can take a scale must be handed the configured one.

`params.pixel_size` is the single owner of every µm conversion in the pipeline. That
only holds if each process that accepts a scale is actually given it: a `bin/` script
whose `--pixel-size` is never passed falls back to its own argparse default, which is a
second, silent owner — exactly the failure this file exists to prevent.

Static rather than behavioural on purpose. `-stub` never evaluates a `script:` block, so
a stub run cannot see a rendered command at all, and a real nf-test per process would
need that process's whole dependency stack (BaSiCPy for PREPROCESS, a STARE manifest for
TILED_STITCH) to be installed just to read a string.
`tests/modules/split_channels.nf.test`'s rendered-command case is the behavioural
counterpart for the one process that can be rendered cheaply.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
MODULES = ROOT / "modules" / "local"
MODULES_CONFIG = ROOT / "conf" / "modules.config"

# ONE pattern for "any spelling of the scale flag", shared by every scan in this file.
# An earlier version of FLAG_RE required the flag to END right after `--pixel[-_]size`,
# so `--pixel-size-um` -- the spelling `bin/warp_seg_qc.py` used -- slipped past by one
# suffix and that script was never covered by the guard written to cover it. The `[a-z_-]*`
# tail is what closes that class of escape: it runs PAST the canonical spelling, so a
# fourth invention (`--pixel-size-microns`, say) is scanned rather than skipped. Nothing
# here enumerates the spellings that happen to exist today -- enumerating them is how the
# hole appeared.
ANY_SPELLING = r"--pixel[-_]size[a-z_-]*"

FLAG_RE = re.compile(r'add_argument\(\s*["\']({})["\']'.format(ANY_SPELLING))
PASSED_RE = re.compile(ANY_SPELLING + r"\s+\$\{([^}]+)\}")

# Scripts that are operator tools rather than pipeline processes: no module invokes
# them, so there is no call site to check. Each must stay unreferenced by modules/ for
# this exemption to remain honest, which is asserted below.
STANDALONE = {"join_flowpath.py", "registration_benchmark.py"}


def _scripts_accepting_a_scale() -> dict[str, str]:
    out = {}
    for path in sorted(BIN.glob("*.py")):
        m = FLAG_RE.search(path.read_text())
        if m:
            out[path.name] = m.group(1)
    return out


# SEGMENT does not name its backend script: it renders `${backend.entrypoint}` from
# lib/SegBackends.groovy, so no .nf file contains the string "segment_instantseg.py".
# Resolved through the same table the module uses, rather than hardcoded here, so a
# renamed entrypoint surfaces as a lookup failure instead of a silently skipped script.
SEG_BACKENDS = ROOT / "lib" / "SegBackends.groovy"


def _backend_dispatched() -> set[str]:
    return set(re.findall(r"entrypoint:\s*'([^']+)'", SEG_BACKENDS.read_text()))


def _call_sites(script: str) -> list[Path]:
    """Module files whose script block invokes this bin script."""
    direct = [
        nf for nf in sorted(MODULES.glob("*.nf")) if f"{script} " in nf.read_text()
    ]
    if direct or script not in _backend_dispatched():
        return direct
    return [MODULES / "segment.nf"]


def _haystack(nf: Path) -> str:
    """The module's own text plus the `withName:` block that supplies its ext.args.

    A flag can legitimately live in either place: `conf/modules.config` is where tool
    arguments belong by convention, and several processes pass the scale from there.
    """
    text = nf.read_text()
    m = re.search(r"^process\s+([A-Z0-9_]+)\s*\{", text, re.M)
    if not m:
        return text
    conf = MODULES_CONFIG.read_text()
    block = re.search(rf"withName:\s*'{m.group(1)}'\s*\{{(.*?)\n    \}}", conf, re.S)
    return text + (block.group(1) if block else "")


def _resolves_to_the_param(expr: str, nf_text: str) -> bool:
    """True if the rendered value is params.pixel_size, directly or via a local def."""
    expr = expr.strip()
    if expr == "params.pixel_size":
        return True
    return bool(
        re.search(rf"def\s+{re.escape(expr)}\s*=\s*params\.pixel_size\b", nf_text)
    )


SCRIPTS = _scripts_accepting_a_scale()


def test_the_scan_found_the_scripts_it_is_meant_to_cover():
    """A scope glob that matches nothing passes vacuously; this is the tripwire."""
    assert {
        "convert_image.py",
        "preprocess.py",
        "split_multichannel.py",
        "tiled_stitch.py",
        "export_geojson.py",
        # Never covered until the FLAG_RE above was widened: it declared the flag as
        # `--pixel-size-um`, one suffix past what the old regex would match, so the scan
        # skipped it entirely and `warp_seg_qc.nf` passed no scale at all.
        "warp_seg_qc.py",
    } <= set(SCRIPTS), sorted(SCRIPTS)


@pytest.mark.parametrize("script", sorted(_scripts_accepting_a_scale()))
def test_every_scale_accepting_script_is_handed_the_configured_scale(script):
    sites = _call_sites(script)
    if script in STANDALONE:
        assert not sites, (
            f"{script} is listed as standalone but {[p.name for p in sites]} invokes "
            f"it — remove it from STANDALONE and check the call site instead"
        )
        return
    assert sites, f"no module invokes {script}; add it to STANDALONE with a reason"
    for nf in sites:
        found = PASSED_RE.search(_haystack(nf))
        assert found, (
            f"{nf.name} invokes {script}, which accepts {SCRIPTS[script]}, but never "
            f"passes it — the script would silently use its own argparse default"
        )
        assert _resolves_to_the_param(found.group(1), nf.read_text()), (
            f"{nf.name} passes {SCRIPTS[script]} as '{found.group(1)}', which does not "
            f"resolve to params.pixel_size — a second owner of the scale"
        )


# ── one spelling ───────────────────────────────────────────────────────────────
# Three spellings were in use at once -- `--pixel_size`, `--pixel-size` and
# `--pixel-size-um` -- and the third is exactly how `bin/warp_seg_qc.py` escaped the
# scan above for as long as it existed. A flag with more than one spelling cannot be
# grepped for, cannot be guarded by one regex, and makes "is the scale passed here?"
# a question you have to answer per file. One spelling, asserted in both directions:
# every declaration uses it, and no call site renders anything else.
CANONICAL_FLAG = "--pixel-size"

ANY_SPELLING_RE = re.compile(ANY_SPELLING)


def _spellings_in(text: str) -> set[str]:
    return set(ANY_SPELLING_RE.findall(text))


def test_every_declaration_uses_the_one_spelling():
    """Each bin/ script's argparse declaration of the scale flag."""
    offenders = {}
    for path in sorted(BIN.glob("*.py")):
        for m in FLAG_RE.finditer(path.read_text()):
            if m.group(1) != CANONICAL_FLAG:
                offenders.setdefault(path.name, set()).add(m.group(1))
    assert not offenders, (
        f"these scripts declare the scale flag under a non-canonical spelling: "
        f"{ {k: sorted(v) for k, v in offenders.items()} }; "
        f"the one spelling is {CANONICAL_FLAG}"
    )


def test_every_call_site_renders_the_one_spelling():
    """Each module file and conf/modules.config `ext.args` that passes the scale."""
    offenders = {}
    for path in sorted(MODULES.glob("*.nf")) + [MODULES_CONFIG]:
        bad = {s for s in _spellings_in(path.read_text()) if s != CANONICAL_FLAG}
        if bad:
            offenders[path.name] = sorted(bad)
    assert not offenders, (
        f"these call sites render a non-canonical spelling of the scale flag: "
        f"{offenders}; the one spelling is {CANONICAL_FLAG}"
    )


def test_the_scan_pattern_covers_a_spelling_nobody_has_invented_yet():
    """The comment on ANY_SPELLING makes a claim; this executes it.

    The previous version of that comment described a `[^\\s'"]*` tail that was not in
    the regex at all, and under it `--pixel-size-microns` escaped FLAG_RE exactly as
    `--pixel-size-um` had. A comment asserting a mechanism the code does not have is the
    same defect this whole file exists to catch, one level up.
    """
    for spelling in (
        "--pixel_size",
        "--pixel-size",
        "--pixel-size-um",
        "--pixel-size-microns",  # nobody's spelling; the point is that it is scanned
        "--pixel_size_um",
    ):
        assert FLAG_RE.search(f'add_argument("{spelling}",'), (
            f"FLAG_RE does not see {spelling}: a script declaring it would be skipped "
            f"by the call-site scan entirely"
        )
        assert FLAG_RE.search(f'add_argument("{spelling}",').group(1) == spelling
        assert PASSED_RE.search(spelling + " ${params.pixel_size}"), (
            f"PASSED_RE does not see {spelling} at a call site"
        )
