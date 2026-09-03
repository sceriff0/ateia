"""No document may show a boolean parameter passed on the command line.

Nextflow 26 hands every `--param` to nf-schema as a **String**. Probed directly with an
isolated `workflow { println params.dry_run.getClass() }`:

    25.04.7   class java.lang.Boolean
    26.04.6   class java.lang.String

so the real pipeline then fails validation with
`* --dry_run (true): Value is [string] but should be [boolean]`. This is not a bump-day
problem: `.github/workflows/ci.yml`'s `nextflow-stub` matrix already includes
`latest-everything`, which is Nextflow 26 today, and that job sits in the blocking
`all-tests` gate.

**Both CLI forms fail.** `--dry_run true` fails, and so does a bare valueless `--dry_run` --
NF26 still delivers a String. The only working forms are `-params-file` with a real JSON
boolean, or a profile. That applies to all 17 boolean parameters in the schema, not just
`dry_run`, which is why this guard derives its parameter list from the schema rather than
hardcoding a name.

A document showing the broken form is a live copy-paste hazard: nothing executes it, so no
test catches it, and the reader's first run fails on a valid pipeline.
"""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SCAN = [
    REPO / "README.md",
    *sorted((REPO / "docs").glob("*.md")),
    *sorted((REPO / "docs" / "figures").glob("*.html")),
    *sorted((REPO / "tests").glob("*.md")),
    *sorted((REPO / "tests").glob("*.sh")),
]

# Files that quote the broken form ON PURPOSE, with the reason. Each is verified below to still
# contain it -- a stale exemption would silently cover a real regression.
ALLOWED = {
    "docs/nextflow-26-bump.md": (
        "the bump write-up; its table enumerates every site that still shows the broken form, "
        "so it must be able to name it"
    ),
    "tests/run_validation_tests.sh": (
        "the header comment explains why the script uses -params-file, quoting the form it "
        "replaced; the executed command uses -params-file"
    ),
}


def _boolean_params():
    schema = json.loads((REPO / "nextflow_schema.json").read_text())
    names = set()

    def walk(node):
        if not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict):
            for name, spec in props.items():
                if not isinstance(spec, dict):
                    continue
                types = spec.get("type")
                types = types if isinstance(types, list) else [types]
                if "boolean" in types:
                    names.add(name)
        for v in node.values():
            walk(v)

    walk(schema)
    assert names, (
        "schema walk found no boolean params -- the guard would pass vacuously"
    )
    return names


# A line may show the broken form as an explicit counter-example -- docs/usage.md's
# "Boolean parameters" section does exactly that -- if it says so ON THE SAME LINE. The marker
# has to be adjacent, not a heading above the block, so it cannot drift away from the line it
# excuses and quietly start covering a working example.
_COUNTEREXAMPLE = re.compile(r"#\s*x\s+fails\b", re.I)


def _offending_lines(text, params):
    pattern = re.compile(rf"--({'|'.join(sorted(params))})\s+(?:true|false)\b")
    for i, line in enumerate(text.splitlines(), start=1):
        if _COUNTEREXAMPLE.search(line):
            continue
        if pattern.search(line):
            yield i, line.strip()


def test_no_document_shows_a_boolean_param_on_the_command_line():
    params = _boolean_params()
    offenders = []

    for f in SCAN:
        rel = f.relative_to(REPO).as_posix()
        if rel in ALLOWED:
            continue
        for line_no, line in _offending_lines(f.read_text(), params):
            offenders.append(f"{rel}:{line_no}: {line}")

    assert not offenders, (
        "Nextflow 26 delivers every --param as a String, so these examples fail validation on "
        "the engine CI already runs. Use -params-file with a real JSON boolean, or a profile:\n  "
        + "\n  ".join(offenders)
    )


def test_every_allowlisted_file_still_quotes_the_form_it_is_exempt_for():
    """A stale exemption is worse than no exemption -- it covers the next real regression."""
    params = _boolean_params()

    for rel in ALLOWED:
        text = (REPO / rel).read_text()
        assert list(_offending_lines(text, params)), (
            f"{rel} no longer shows a CLI boolean, so its ALLOWED entry is stale. "
            "Remove the entry rather than leaving it to cover a future regression."
        )


def test_the_scan_covers_the_documents_this_guard_exists_for():
    scanned = {f.relative_to(REPO).as_posix() for f in SCAN}

    for expected in ("README.md", "docs/usage.md", "tests/test_validation.md"):
        assert expected in scanned


def test_the_detector_recognises_the_form_that_actually_fails():
    params = _boolean_params()

    assert list(_offending_lines("  --start preprocessing --dry_run true\n", params))
    assert list(_offending_lines("nextflow run . --skip_preprocessing false\n", params))


def test_the_detector_leaves_the_working_forms_alone():
    params = _boolean_params()

    assert not list(
        _offending_lines("nextflow run . -params-file params/dry_run.json\n", params)
    )
    assert not list(_offending_lines("nextflow run . -profile test,docker\n", params))
    # a non-boolean param legitimately takes a CLI value
    assert not list(_offending_lines("nextflow run . --start preprocessing\n", params))


def test_the_counterexample_marker_must_be_on_the_same_line():
    """A marker that could sit on a heading would drift and start excusing working examples."""
    params = _boolean_params()

    marked = "nextflow run . --dry_run true   # x fails on Nextflow 26\n"
    assert not list(_offending_lines(marked, params))

    # the same command one line below a marker is NOT excused
    detached = "# x fails on Nextflow 26\nnextflow run . --dry_run true\n"
    assert list(_offending_lines(detached, params))


def test_no_shell_script_passes_a_boolean_param_on_the_command_line():
    """The docs check above is advisory; a script is EXECUTED, so the same mistake
    there is live rather than misleading.

    benchmarks/submit_arms.sh shipped `--skip_seg_quality_eval false`, which on
    Nextflow 26 sets the param to the STRING "false" -- truthy -- and so disabled
    the scorer while reading like it enabled it. Use a profile (`seg_quality`) or
    -params-file with a real JSON boolean.

    SCOPE IS EVERY TRACKED .sh, not one directory. The original version globbed
    benchmarks/ only; that directory lives on the `benchmarking` branch alone, so
    on `dev` the glob matched nothing and the assertion passed vacuously -- a
    guard that cannot fail is not a guard. `git ls-files` is the enumerator
    because it follows the branch: whatever shell scripts a branch actually
    ships are the ones checked.
    """
    import re
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    bools = _boolean_params()  # noqa: F821 - defined above in this module

    listed = subprocess.run(
        ["git", "ls-files", "*.sh"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    scripts = sorted(root / rel for rel in listed)
    assert scripts, (
        "found no tracked .sh files -- the enumerator is wrong, not the repo"
    )

    offenders = []
    for sh in scripts:
        for i, line in enumerate(sh.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for m in re.finditer(r"--(\w+)\s+(true|false)\b", line):
                if m.group(1) in bools:
                    offenders.append(f"{sh.relative_to(root)}:{i}: {line.strip()}")
    assert not offenders, (
        "shell scripts pass a boolean param on the command line:\n  "
        + "\n  ".join(offenders)
    )
