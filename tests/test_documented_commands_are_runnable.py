r"""Every `nextflow run` command in the docs must survive launch validation.

Three parameters have no default and are required by `nextflow_schema.json`:
`outdir`, `max_cpus` and `max_memory`. nextflow.config declares the last two
`null` deliberately -- see the comment above `max_cpus = null`. A command that
supplies none of them is refused before any process is submitted:

    ERROR ~ Validation of pipeline parameters failed!
    * Missing required parameter(s): max_cpus, max_memory

25 of the 32 `nextflow run` commands in README.md and docs/*.md were in exactly
that state on 2026-09-02, including the one the Usage page calls "the minimal
command". Nothing executes a fenced code block, so no test caught it and the
reader's first run failed on a perfectly good pipeline.

Ruling R4 for 1.0.0: a documented command satisfies the sizing pair through
`-c site.config` (copied from conf/site.config.template, which carries them),
through a profile that pins them, or through a `-params-file` preset that
carries them. This guard reads the commands the way a reader does -- out of the
fenced code blocks, following backslash continuations -- so it fails on the
copy-paste the reader would actually make.
"""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SCAN = [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))]

# Profiles that pin BOTH params.max_cpus and params.max_memory, and those that
# additionally pin params.outdir. Both sets are verified against the config below,
# so a profile that stops pinning cannot stay on the list.
SIZING_PROFILES = {"test", "test_full", "local", "ieo", "shipped_defaults_test"}
OUTDIR_PROFILES = {"test", "test_full", "shipped_defaults_test"}

_FENCE = re.compile(r"^\s*(```|~~~)")
# A line may show a deliberately broken command if it says so ON THE SAME LINE --
# docs/usage.md's "Boolean parameters" section does exactly that.
_COUNTEREXAMPLE = re.compile(r"#\s*x\s+fails\b", re.I)
_TRAILING_COMMENT = re.compile(r"\s+#.*$")


def _commands(text):
    """Yield (line_no, command) for every `nextflow run` inside a fenced block.

    Backslash continuations are joined, so a command spread over six lines is one
    command -- which is how the reader pastes it.
    """
    in_fence = False
    buf = None
    start = None
    for line_no, line in enumerate(text.splitlines(), start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
            if not in_fence and buf is not None:
                yield start, buf
                buf = None
            continue
        if not in_fence:
            continue
        stripped = line.strip().removeprefix("$ ")
        if buf is None:
            if not stripped.startswith("nextflow run"):
                continue
            if _COUNTEREXAMPLE.search(line):
                continue
            buf, start = stripped, line_no
        else:
            buf = buf[:-1] + " " + stripped
        if buf.rstrip().endswith("\\"):
            continue
        yield start, buf
        buf = None
    if buf is not None:
        yield start, buf


def _profile_body(name):
    """The text a `-profile <name>` actually contributes, includes resolved."""
    text = (REPO / "nextflow.config").read_text()
    match = re.search(rf"^\s+{re.escape(name)}\s*\{{", text, re.M)
    if not match:
        return ""
    depth = 0
    start = text.index("{", match.start())
    end = None
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = text[start : end + 1] if end else text[start:]
    for included in re.findall(r"includeConfig\s+'([^']+)'", body):
        path = REPO / included
        if path.is_file():
            body += "\n" + path.read_text()
    return body


def _profile_pins(name, param):
    return bool(re.search(rf"^\s*(params\.)?{param}\s*=", _profile_body(name), re.M))


def _preset(path_str):
    path = REPO / path_str
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def _verdict(command):
    """The reasons this command would be refused at launch, or []."""
    command = _TRAILING_COMMENT.sub("", command)

    profiles = set()
    for match in re.finditer(r"-profile\s+(\S+)", command):
        profiles.update(match.group(1).split(","))

    preset = {}
    for name in re.findall(r"-params-file\s+(\S+)", command):
        preset.update(_preset(name))

    has_c = re.search(r"(^|\s)-c\s+\S+", command) is not None

    reasons = []
    if not (
        "--outdir" in command
        or (profiles & OUTDIR_PROFILES)
        or "outdir" in preset
        or has_c
    ):
        reasons.append("no --outdir (required, no default)")
    if not (
        (profiles & SIZING_PROFILES)
        or has_c
        or ("max_cpus" in preset and "max_memory" in preset)
        or ("--max_cpus" in command and "--max_memory" in command)
    ):
        reasons.append("no max_cpus/max_memory (required, no default)")
    return reasons


def test_every_documented_command_would_launch():
    offenders = []
    total = 0
    for path in SCAN:
        rel = path.relative_to(REPO).as_posix()
        for line_no, command in _commands(path.read_text()):
            total += 1
            reasons = _verdict(command)
            if reasons:
                offenders.append(
                    f"{rel}:{line_no}: {'; '.join(reasons)}\n      {command}"
                )
    assert total >= 25, (
        f"only {total} `nextflow run` commands were extracted from {len(SCAN)} files -- "
        "the extractor has stopped matching and this scan read almost nothing"
    )
    assert not offenders, (
        "documented commands that nf-schema refuses at launch. Add `-c site.config` "
        "(conf/site.config.template) and/or `--outdir`:\n  " + "\n  ".join(offenders)
    )


def test_the_profiles_this_guard_trusts_really_pin_what_it_claims():
    """A profile silently dropping its `max_*` would make this guard accept a
    command that no longer launches."""
    for name in sorted(SIZING_PROFILES):
        assert _profile_pins(name, "max_cpus"), (
            f"profile `{name}` no longer pins max_cpus"
        )
        assert _profile_pins(name, "max_memory"), (
            f"profile `{name}` no longer pins max_memory"
        )
    for name in sorted(OUTDIR_PROFILES):
        assert _profile_pins(name, "outdir"), f"profile `{name}` no longer pins outdir"


def test_the_extractor_joins_continuations_and_skips_prose():
    text = "\n".join(
        [
            "Some prose naming `nextflow run . --input x` outside a fence.",
            "```bash",
            "nextflow run . \\",
            "  --input samplesheet.csv \\",
            "  --outdir results",
            "```",
        ]
    )
    found = list(_commands(text))
    assert len(found) == 1, found
    line_no, command = found[0]
    assert line_no == 3
    assert command == "nextflow run .  --input samplesheet.csv  --outdir results"


def test_the_detector_recognises_the_command_that_actually_fails():
    assert _verdict(
        "nextflow run . --input s.csv --outdir results -profile docker"
    ) == ["no max_cpus/max_memory (required, no default)"]
    assert _verdict("nextflow run . --input s.csv -profile docker") == [
        "no --outdir (required, no default)",
        "no max_cpus/max_memory (required, no default)",
    ]


def test_the_detector_leaves_the_working_forms_alone():
    assert _verdict("nextflow run . -profile test,docker --outdir results") == []
    assert (
        _verdict(
            "nextflow run . --input s.csv --outdir r -profile docker -c site.config"
        )
        == []
    )
    assert (
        _verdict(
            "nextflow run . --input s.csv --outdir r --max_cpus 8 --max_memory 32.GB"
        )
        == []
    )
