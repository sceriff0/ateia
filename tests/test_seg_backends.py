#!/usr/bin/env python3
"""Static guards for the segmentation backend seam.

`SEGMENT` dispatches three genuinely different segmenters. That choice used to be
re-decided in four places -- a nested `container` ternary, an `if/else if/else` over
three near-duplicate script blocks, a per-backend block of flag construction, and
`conf/modules.config` -- so a fourth backend, or a retired one, had to be tracked down
in all four. `lib/SegBackends.groovy` is now the single table, and this test pins the
properties that make it safe to be single:

  * every backend names a container, an entry point and its own tool list for
    versions.yml -- as BARE MODULE NAMES, not rendered YAML, so segment.nf's script:
    and stub: blocks can both render it through lib/ProcessEnvelope.groovy;
  * the entry points exist and are tracked EXECUTABLE (Nextflow stages `bin/` onto
    $PATH and execs them by name -- a 100644 script fails at runtime with exit 126,
    `Permission denied`, and only on the cluster's fresh checkout);
  * container tags are immutable, never `:latest`;
  * `SEGMENT` has an `ext.args` entry that covers every backend the table declares.

These run in the plain pytest job (no Nextflow, no container engine), so they hold
even when the nf-test suite's real-execution cases are skipped for lack of Docker.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKENDS_GROOVY = ROOT / "lib" / "SegBackends.groovy"
MODULES_CONFIG = ROOT / "conf" / "modules.config"
SEGMENT_NF = ROOT / "modules" / "local" / "segment.nf"

# The backends the pipeline ships, and the image each MUST use. Written out rather
# than parsed so a silent retag (the class of change that turns a reproducible run
# into an unreproducible one) shows up as a test diff.
EXPECTED = {
    "stardist": (
        "bolt3x/attend_image_analysis:segmentation_gpu",
        "segment.py",
        ("deepcell", "tensorflow"),
    ),
    "instantseg": (
        "bolt3x/attend_image_analysis:instant_seg",
        "segment_instantseg.py",
        ("instanseg", "torch"),
    ),
    "cellsam": (
        "bolt3x/attend_image_analysis:cellsam",
        "segment_cellsam.py",
        ("cellSAM", "torch"),
    ),
}


def _backend_blocks() -> dict[str, str]:
    """Split SegBackends.groovy's table into {method: raw text of its entry}."""
    text = BACKENDS_GROOVY.read_text()
    starts = {}
    for method in EXPECTED:
        m = re.search(rf"^\s*{method}\s*:\s*\[", text, re.MULTILINE)
        assert m, f"no `{method}:` entry in {BACKENDS_GROOVY.name}"
        starts[method] = m.start()
    ordered = sorted(starts.items(), key=lambda kv: kv[1])
    blocks = {}
    for i, (method, start) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else len(text)
        blocks[method] = text[start:end]
    return blocks


def test_every_backend_declares_its_container_and_entrypoint():
    blocks = _backend_blocks()
    for method, (container, entrypoint, _versions) in EXPECTED.items():
        block = blocks[method]
        assert f"'{container}'" in block, (
            f"seg_method '{method}' must use container {container}"
        )
        assert f"'{entrypoint}'" in block, (
            f"seg_method '{method}' must invoke {entrypoint}"
        )


VERSION_TOOLS_RE = re.compile(r"versionTools\s*:\s*\[([^\]]*)\]")


def _version_tools(block: str) -> tuple[str, ...]:
    """The backend's declared tool list, parsed from its `versionTools: [...]` entry."""
    m = VERSION_TOOLS_RE.search(block)
    assert m, "backend entry declares no versionTools list"
    return tuple(re.findall(r"'([^']*)'", m.group(1)))


def test_versions_rows_stay_backend_specific():
    """A shared versions.yml would report the wrong stack for two of three backends.

    Exact ORDERED equality, not containment: `torch` is named by both instantseg and
    cellsam, so a containment check would still pass if the two lists were merged into
    one shared list and handed to all three backends -- which is precisely the mistake
    this test exists to refuse, since stardist loads no torch at all and reporting a
    torch version for a StarDist run is a fabricated provenance record. Exactly one
    backend runs per task, so the repeated `torch` entry is not a duplicate to dedupe.
    """
    blocks = _backend_blocks()
    for method, (_container, _entrypoint, versions) in EXPECTED.items():
        assert _version_tools(blocks[method]) == versions, (
            f"seg_method '{method}' must declare versionTools {list(versions)}"
        )


def test_backends_declare_tool_names_not_rendered_yaml():
    """The rendering belongs to lib/ProcessEnvelope.groovy, and only there.

    This table used to hand-write six probe strings of exactly the form
    ProcessEnvelope.probe() renders. Because they were pre-rendered YAML lines, they
    could not be reused by segment.nf's stub: block (a stub reports no real versions),
    so that block hand-wrote a DIFFERENT set of keys and `-stub` -- the whole blocking
    CI gate -- could never see the two disagree. Storing bare module names is what makes
    one list serve both blocks, so a reintroduced probe string here is the regression.
    """
    text = BACKENDS_GROOVY.read_text()
    assert "python -c" not in text, (
        "lib/SegBackends.groovy must declare tool NAMES (versionTools: ['deepcell', ...]), "
        "not pre-rendered version-probe shell. ProcessEnvelope.probe() renders the probe, "
        "which is what lets segment.nf's script: and stub: blocks share one list."
    )
    assert "END_VERSIONS" not in text, (
        "the versions.yml heredoc belongs to ProcessEnvelope, not to the backend table"
    )


def test_segment_renders_both_blocks_from_the_one_backend_list():
    """segment.nf's script: and stub: must each render versions.yml from
    `backend.versionTools`, and nothing else.

    tests/test_versions_envelope.py proves this generically for every module (identical
    ProcessEnvelope call text, identical `*Backends.of(...)` binding in both halves).
    This case names segment.nf specifically because it was the LAST holdout and the
    single clearest live instance of the divergence: script: reported the backend's real
    tools while stub: reported a bare `seg_method: <name>` that is not a tool at all.
    """
    text = SEGMENT_NF.read_text()
    assert "END_VERSIONS" not in text, (
        "segment.nf must not hand-write a versions.yml heredoc in either block"
    )
    assert (
        "ProcessEnvelope.versions(task.process, backend.versionTools)" in text
        and "ProcessEnvelope.versionsStub(task.process, backend.versionTools)" in text
    ), "segment.nf must render BOTH blocks from backend.versionTools"
    # Both blocks must resolve `backend` from the same expression, or two identical
    # `backend.versionTools` reads still name two different backends' tools.
    bindings = re.findall(r"=\s*(SegBackends\.of\([^)\n]*\))", text)
    assert len(bindings) == 2 and bindings[0] == bindings[1] == "SegBackends.of(params.seg_method)", (
        f"segment.nf must bind `backend` from the same SegBackends.of(...) expression in "
        f"script: and stub:, found: {bindings}"
    )


def test_container_tags_are_immutable():
    text = BACKENDS_GROOVY.read_text()
    assert ":latest" not in text, (
        "segmentation containers must be pinned to an immutable tag, never :latest"
    )
    for container, _entrypoint, _versions in EXPECTED.values():
        assert container.count(":") == 1 and not container.endswith(":"), (
            f"{container} must carry an explicit tag"
        )


def test_entrypoints_exist_and_are_tracked_executable():
    """`bin/` scripts invoked BY NAME must be git-mode 100755, not merely chmod +x.

    Nextflow stages `bin/` onto $PATH and execs the script directly; a 100644 mode in
    the index means the cluster's checkout gets a non-executable file and the task
    dies with exit 126 long after the local run looked fine.
    """
    for _container, entrypoint, _versions in EXPECTED.values():
        path = ROOT / "bin" / entrypoint
        assert path.exists(), f"{entrypoint} named by SegBackends does not exist"
        mode = subprocess.run(
            ["git", "ls-files", "-s", f"bin/{entrypoint}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        assert mode and mode[0] == "100755", (
            f"bin/{entrypoint} is git-mode {mode[0] if mode else 'untracked'}; "
            f"it is invoked by name so it must be 100755 "
            f"(git update-index --chmod=+x bin/{entrypoint})"
        )


def test_segment_has_an_ext_args_entry_covering_every_backend():
    """SEGMENT had no `ext.args` at all, so `task.ext.args ?: ''` was always empty."""
    text = MODULES_CONFIG.read_text()
    start = text.find("withName: 'SEGMENT' {")
    assert start != -1, "no withName: 'SEGMENT' block in conf/modules.config"
    # Bound the block at the next withName so a neighbour's ext.args cannot satisfy this.
    nxt = text.find("withName:", start + 1)
    block = text[start : nxt if nxt != -1 else len(text)]
    assert "ext.args" in block, "SEGMENT must define ext.args (CLAUDE.md convention)"
    for method in EXPECTED:
        assert re.search(rf"\b{method}\s*:", block), (
            f"SEGMENT's ext.args must cover seg_method '{method}'"
        )


def test_segment_process_body_is_backend_agnostic():
    """One script block, and no parameter reads other than the dispatch key.

    The whole point of the table is that the process stops re-deciding the backend.
    A reintroduced `if (params.seg_method == ...)` in the script, or a new
    `params.seg_<tunable>` read, is that decision leaking back out of the seam --
    tunables belong in conf/modules.config's ext.args.
    """
    text = SEGMENT_NF.read_text()
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("*")
    )
    reads = set(re.findall(r"params\.([A-Za-z_][A-Za-z0-9_]*)", code))
    assert reads == {"seg_method"}, (
        f"segment.nf should read only params.seg_method, found: {sorted(reads)}"
    )
    # `script:` ... `stub:` -- exactly two triple-quoted blocks in the whole file.
    assert code.count('"""') == 4, (
        "segment.nf must have exactly one script block and one stub block"
    )
