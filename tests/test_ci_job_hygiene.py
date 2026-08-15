#!/usr/bin/env python3
"""Guard: CI job hygiene — every job bounds its own runtime, and no job installs
Docker buildx it never uses.

`.github/workflows/ci.yml` used to declare no `timeout-minutes` on any job, so a
hung job (a wedged `nextflow run`, a stuck `nf-test`, ...) would burn the full
6-hour GitHub Actions runner default before the run was killed. Separately, two
jobs (`nextflow-stub`, `nf-test-stub`) install `docker/setup-buildx-action` even
though both run entirely container-free (`-profile test`, no `docker` profile in
any of their own run steps) — a `Set up Docker` step that does nothing but slow
the job down and give a false impression that a container engine matters here.

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
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _load_jobs() -> dict:
    data = yaml.safe_load(CI_WORKFLOW.read_text())
    jobs = data.get("jobs")
    assert jobs, f"no `jobs:` block found in {CI_WORKFLOW.relative_to(ROOT)}"
    return jobs


def test_every_job_declares_a_timeout():
    """A job with no `timeout-minutes` inherits GitHub Actions' 360-minute
    (6-hour) default — a hung `nextflow run` or `nf-test` invocation burns the
    whole runner budget before anything kills it. Every job must bound its own
    runtime explicitly."""
    jobs = _load_jobs()
    missing = sorted(
        name for name, job in jobs.items() if "timeout-minutes" not in job
    )
    assert not missing, (
        f"job(s) {missing} in {CI_WORKFLOW.relative_to(ROOT)} declare no "
        "`timeout-minutes` and would inherit GitHub Actions' 360-minute "
        "default if they ever hang. Add an explicit timeout to each."
    )


def test_buildx_job_must_use_a_docker_profile_in_its_own_steps():
    """A job that installs `docker/setup-buildx-action` but never runs anything
    with a `docker` profile (or `-with-docker`) has installed a container engine
    it doesn't use. This states the rule generally — a job using buildx must
    reference a docker profile in one of its own run steps — rather than naming
    today's offending jobs by name, so it keeps working when jobs are added or
    renamed.
    """
    jobs = _load_jobs()
    docker_profile_re = re.compile(r"-profile\s+[^\s]*docker|-with-docker")

    offenders = []
    for name, job in jobs.items():
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
        if not references_docker:
            offenders.append(name)

    assert not offenders, (
        f"job(s) {sorted(offenders)} install docker/setup-buildx-action but "
        "none of their own run steps reference a docker profile "
        "(`-profile ...docker` or `-with-docker`) — the buildx setup step is "
        "dead weight and should be removed."
    )
