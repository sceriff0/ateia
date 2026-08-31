"""The DISK/LightGlue checkpoints must be baked into the :tiled image.

kornia downloads them at FIRST USE into $TORCH_HOME/hub/checkpoints. The cluster has a
READ-ONLY $HOME and docs/usage.md documents air-gapped execution, so a first cluster run
would die exactly the way the VALIS JVM-cache issue did. This is a CONTAINER-ONLY fix: a
git pull can never repair a missing checkpoint.

Verified 2026-08-30: with TORCH_HOME redirected, DISK.from_pretrained("depth") and
LightGlue("disk") write exactly depth-save.pth and disk_lightglue_v0-1_arxiv-pth, and
re-loading both with HOME=/nonexistent succeeds.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TILED_DOCKERFILE = REPO / "containers" / "tiled" / "Dockerfile"


def _dockerfile_code():
    """The Dockerfile with COMMENT LINES REMOVED, so prose can never satisfy a guard.

    Not a refinement -- a defect found by breaking these guards deliberately before
    trusting them. Two of the four stayed GREEN when the thing they protect was deleted,
    because the Dockerfile's comments name the very strings they grep for: the comment
    above the bake explains that `disk_lightglue_v0-1_arxiv-pth` has no dot before `pth`,
    which satisfied `test_tiled_dockerfile_asserts_the_checkpoints_landed` on its own even
    with both `test -f` lines removed. A guard that a comment can satisfy is an annotation.
    """
    text = TILED_DOCKERFILE.read_text()
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    # A check whose "good" answer is an absence must prove it looked at something. If the
    # stripper ever eats the whole file, every assertion below would fail loudly rather
    # than pass vacuously -- but this says so directly.
    assert "FROM python:" in code and "RUN pip install" in code, (
        "the comment stripper returned something that is not a Dockerfile any more; every "
        "assertion in this file would be testing an empty string"
    )
    return code


def test_tiled_dockerfile_sets_torch_home_to_a_world_readable_path():
    text = _dockerfile_code()
    assert "ENV TORCH_HOME=/opt/torch" in text, (
        "containers/tiled/Dockerfile must set TORCH_HOME to a path outside $HOME. "
        "Without it kornia writes checkpoints to ~/.cache/torch at RUN time, which "
        "fails on the cluster's read-only $HOME."
    )


def test_tiled_dockerfile_prefetches_both_checkpoints_at_build_time():
    text = _dockerfile_code()
    assert 'DISK.from_pretrained("depth")' in text, (
        "the DISK checkpoint is not fetched at BUILD time"
    )
    assert 'LightGlue("disk")' in text, (
        "the LightGlue checkpoint is not fetched at BUILD time"
    )


def test_tiled_dockerfile_asserts_the_checkpoints_landed():
    """A prefetch that silently no-ops leaves the same broken image. The build must
    assert the two files exist, by name, after fetching them.

    The assertion is on the full `test -f <path>` form, not on the bare filename: the
    filename alone also appears in the comment that explains its missing dot, and that
    comment kept this test green with both `test -f` lines deleted.
    """
    text = _dockerfile_code()
    for fname in ("depth-save.pth", "disk_lightglue_v0-1_arxiv-pth"):
        assert f"test -f /opt/torch/hub/checkpoints/{fname}" in text, (
            f"containers/tiled/Dockerfile never runs `test -f "
            f"/opt/torch/hub/checkpoints/{fname}`, so the build does not verify the "
            "checkpoint actually landed. Note the LightGlue filename has no dot before "
            "'pth' -- it is derived from the release URL."
        )


def test_tiled_dockerfile_proves_the_cache_works_without_home():
    """The whole point is that it loads with a read-only/absent $HOME."""
    text = _dockerfile_code()
    assert "HOME=/nonexistent" in text, (
        "the build never proves the baked cache is reachable without $HOME, which is "
        "the exact failure mode this bake exists to prevent"
    )


def test_stare_ml_container_is_gone():
    assert not (REPO / "containers" / "stare-ml").exists(), (
        "containers/stare-ml was folded into containers/tiled and must be deleted"
    )
