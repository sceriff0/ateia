#!/usr/bin/env python3
"""Guard: workflow job hygiene — every job bounds its own runtime, and no job
sets up Docker buildx it never uses.

`.github/workflows/ci.yml` used to declare no `timeout-minutes` on any job, so a
hung job (a wedged `nextflow run`, a stuck `nf-test`, ...) would burn the full
6-hour GitHub Actions runner default before the run was killed. Separately, two
jobs (`nextflow-stub`, `nf-test-stub`) installed `docker/setup-buildx-action`
even though both run entirely container-free (`-profile test`, no `docker`
profile in any of their own run steps) — a `Set up Docker` step that does
nothing but slow the job down and give a false impression that a container
engine matters here.

SCOPE: every workflow under `.github/workflows/`, discovered by glob — NOT a
hardcoded `ci.yml`. The hardcoded version of this guard passed green while
`.github/workflows/release.yml` declared no `timeout-minutes` on a single one of
its jobs and set up buildx in a test gate that runs `-profile test` (no docker).
Do not reintroduce a filename list here.

This test needs PyYAML to parse `jobs:` structurally rather than by regex — the
brief is explicit that a hand-rolled YAML parser is not wanted here.
`pytest.importorskip` is used per that guidance, but an importorskip that always
skips guards nothing: PyYAML is importable in this repo's ambient interpreter
(verified `python3 -c "import yaml"` -> 6.0.2), so this test genuinely runs
rather than silently no-op'ing everywhere it matters.

Plain pytest, no pipeline-runtime imports, all paths derived from `__file__` per
this repo's other guard tests (see test_layout.py, test_ci_stack_pinned.py).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"


def workflow_files() -> list[Path]:
    """Every workflow under `.github/workflows/`, DISCOVERED BY GLOB."""
    files = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))
    assert files, f"no workflow files found under {WORKFLOWS_DIR.relative_to(ROOT)}"
    return files


def _load_jobs(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    jobs = data.get("jobs")
    assert jobs, f"no `jobs:` block found in {path.relative_to(ROOT)}"
    return jobs


def test_every_job_declares_a_timeout():
    """A job with no `timeout-minutes` inherits GitHub Actions' 360-minute
    (6-hour) default — a hung `nextflow run` or `nf-test` invocation burns the
    whole runner budget before anything kills it. Every job must bound its own
    runtime explicitly.

    Reusable-workflow calls (a job whose body is `uses: ./.github/workflows/x.yml`)
    are the one exception, and not by exemption: GitHub Actions *rejects*
    `timeout-minutes` on such a job. The bound is owned by the jobs of the called
    workflow, and since this guard globs every workflow file, those jobs are
    themselves in scope here.
    """
    missing = []
    for wf in workflow_files():
        for name, job in _load_jobs(wf).items():
            if "uses" in job:
                # Reusable-workflow call: timeout lives in the called workflow,
                # which this glob already covers.
                continue
            if "timeout-minutes" not in job:
                missing.append(f"{wf.relative_to(ROOT)}: {name}")
    assert not missing, (
        "job(s) declare no `timeout-minutes` and would inherit GitHub Actions' "
        "360-minute default if they ever hang:\n" + "\n".join(sorted(missing)) + "\n"
        "Add an explicit timeout to each."
    )


def test_buildx_job_must_actually_use_buildx():
    """A job that sets up `docker/setup-buildx-action` must actually use it.

    Two things count as using it, and they are the only two things buildx is for
    in this repo:

    * running something with a `docker` profile in one of the job's own run
      steps (`-profile ...docker` / `-with-docker`), i.e. a pipeline run that
      needs a container engine; or
    * a step that builds or pushes an image with a Docker action
      (`docker/build-push-action`, `docker/login-action`), i.e. the job's whole
      purpose IS the image build.

    The second clause is a refinement forced by widening this guard past
    `ci.yml`: `build-images.yml`'s matrix job is the legitimate buildx user in
    this repo — it never runs a pipeline at all, so a "must reference a docker
    profile" rule would have flagged it for doing exactly the thing buildx
    exists to do. Exempting the *file* would have re-opened the hole this
    widening closed; refining the *rule* to "buildx must be used, by either of
    its two real uses" keeps every job in scope. A job matching neither clause
    has set up a container engine it does not use.
    """
    docker_profile_re = re.compile(r"-profile\s+[^\s]*docker|-with-docker")
    docker_build_actions = ("docker/build-push-action", "docker/login-action")

    offenders = []
    for wf in workflow_files():
        for name, job in _load_jobs(wf).items():
            steps = job.get("steps") or []
            uses_buildx = any(
                "docker/setup-buildx-action" in str(step.get("uses", ""))
                for step in steps
            )
            if not uses_buildx:
                continue
            references_docker = any(
                docker_profile_re.search(step.get("run", ""))
                for step in steps
                if "run" in step
            )
            builds_an_image = any(
                any(a in str(step.get("uses", "")) for a in docker_build_actions)
                for step in steps
            )
            if not references_docker and not builds_an_image:
                offenders.append(f"{wf.relative_to(ROOT)}: {name}")

    assert not offenders, (
        "job(s) set up docker/setup-buildx-action but neither run anything with "
        "a docker profile (`-profile ...docker` / `-with-docker`) nor build an "
        "image with a Docker action:\n" + "\n".join(sorted(offenders)) + "\n"
        "The buildx setup step is dead weight and should be removed."
    )
