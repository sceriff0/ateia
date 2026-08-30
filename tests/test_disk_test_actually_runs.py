"""The DISK front-end test must actually RUN in CI, not skip.

tests/test_coarse_frontend.py guards its DISK case with importorskip("torch"). Before
this task CI had no torch, so that case skipped silently -- and a silent skip is how an
unimplemented front-end shipped in the first place. This asserts CI installs torch and
kornia, so the skip cannot come back unnoticed.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"


def _ci_text():
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert files, f"no workflow files under {WORKFLOWS}"
    return "\n".join(f.read_text() for f in files)


def test_ci_installs_torch_and_kornia():
    text = _ci_text()
    assert re.search(r"pip install[^\n]*\btorch==2\.3\.1\b", text), (
        "no workflow installs torch==2.3.1, so tests/test_coarse_frontend.py's DISK "
        "case skips in CI and proves nothing"
    )
    assert re.search(r"pip install[^\n]*\bkornia==0\.7\.3\b", text), (
        "no workflow installs kornia==0.7.3"
    )


def test_ci_installs_the_cpu_torch_wheel():
    """A CUDA wheel is ~2.5 GB and pointless on a CPU runner; the shipped container is
    CPU-only, so CI must exercise the same wheel."""
    text = _ci_text()
    assert "download.pytorch.org/whl/cpu" in text, (
        "torch must be installed from the CPU index-url, matching containers/tiled"
    )
