#!/usr/bin/env python3
"""Guard: one commit must flow through the whole release.

`.github/workflows/release.yml` used to build a release from THREE different
commits, and every job in it was green while doing so:

  * `test` (the gate) did a BARE `actions/checkout@v4`, i.e. `github.ref` — the
    branch the workflow_dispatch was fired from;
  * `bump-and-tag` checked out `ref: main`, bumped `manifest.version` there,
    committed and tagged THAT, regardless of the dispatch ref; and
  * `artifacts`, `publish-images` and `verify-config` all carried
    `needs: [validate, test]` — NOT `needs: bump-and-tag` — and did a bare
    checkout of their own.

Because `needs` is what orders jobs, `artifacts` and `publish-images` ran IN
PARALLEL with the version bump, from the UN-BUMPED tree. `mirage-v1.0.0.tar.gz`
would have shipped a `nextflow.config` still reading the previous version, and
the ten container images — whose tags are exactly `manifest.version`, which
`modules/local/*.nf` pull by name — would have been built from that same stale
tree. Nothing in the run would have looked wrong.

So the invariants below are about the SHAPE of the job graph, which is the only
place this defect is visible:

  1. every `actions/checkout` in release.yml names an explicit `ref`, drawn from
     a small allowed vocabulary — a bare checkout is the bug;
  2. every gate job checks out the SAME pinned SHA that `validate` resolved;
  3. the jobs that produce release artifacts depend on `bump-and-tag` and build
     from the CREATED TAG;
  4. those same jobs tolerate `bump-and-tag` being SKIPPED, because it is
     skipped on the `release: published` path and a job whose dependency is
     skipped is itself skipped — so without `always()` this fix would silently
     delete the artifact and image jobs from that trigger entirely; and
  5. the gate actually runs CI's blocking checks, asserted by the commands
     themselves rather than by job name.

Every "must not find anything" assertion here is paired with one proving a real
graph was parsed, per this repo's convention for zero-answer checks.

Plain pytest, paths derived from `__file__`, per the other guard tests in this
directory (test_ci_stack_pinned.py, test_ci_job_hygiene.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
CI = WORKFLOWS / "ci.yml"
BUILD_IMAGES = WORKFLOWS / "build-images.yml"

# The reusable image-build workflow, exactly as release.yml must name it.
BUILD_IMAGES_USES = "./.github/workflows/build-images.yml"

# `validate` resolves these; everything downstream reads them. Expressed as the
# expression text the workflow must contain, because that is what actually has
# to match for the checkout to land on the right commit.
PINNED_SHA = "needs.validate.outputs.sha"
RELEASE_TAG = "needs.validate.outputs.version"
GATE_REF = "steps.get_version.outputs.gate_ref"


def load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), f"{path.relative_to(ROOT)} did not parse to a mapping"
    return data


def jobs_of(path: Path) -> dict:
    jobs = load(path).get("jobs")
    assert jobs, f"no `jobs:` block in {path.relative_to(ROOT)}"
    return jobs


def needs_of(job: dict) -> list[str]:
    """`needs:` normalised to a list — GitHub accepts a bare string too."""
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def run_text(job: dict) -> str:
    return "\n".join(
        str(step.get("run", "")) for step in (job.get("steps") or []) if "run" in step
    )


def checkout_steps(job: dict) -> list[dict]:
    return [
        step
        for step in (job.get("steps") or [])
        if "actions/checkout" in str(step.get("uses", ""))
    ]


def release_jobs() -> dict:
    return jobs_of(RELEASE)


def artifact_producing_jobs() -> dict:
    """Jobs whose OUTPUT is a release artifact — DISCOVERED, never named.

    Two kinds, and they are the two things a release ships:
      * the job that calls the reusable image build (`uses:` build-images.yml); and
      * any job that tars up `mirage-<version>.tar.gz`.

    Naming them instead would let a third artifact job be added tomorrow with a
    bare checkout and no `bump-and-tag` dependency, and this file would not
    notice — which is precisely how `artifacts` and `publish-images` came to be
    wired to the wrong commit in the first place.
    """
    found = {}
    for job_id, job in release_jobs().items():
        if not isinstance(job, dict):
            continue
        if BUILD_IMAGES_USES in str(job.get("uses", "")):
            found[job_id] = job
            continue
        text = run_text(job)
        if "tar " in text and "mirage-" in text:
            found[job_id] = job
    return found


def gate_jobs() -> dict:
    """The jobs the `test` aggregator depends on — read off its `needs:`."""
    jobs = release_jobs()
    assert "test" in jobs, (
        "release.yml has no `test` job. Everything downstream is gated on it by name; "
        "if it was renamed, this whole file is checking a graph that no longer exists."
    )
    ids = needs_of(jobs["test"])
    return {job_id: jobs[job_id] for job_id in ids if job_id in jobs}


# ---------------------------------------------------------------------------
# Non-vacuity: the graph parsed, and it is the graph this file is about.
# ---------------------------------------------------------------------------


def test_the_release_graph_parsed_and_has_the_jobs_this_file_reasons_about():
    """Every assertion below is a lookup into this graph. If the parse yields an
    empty or unrecognisable one, they all pass having checked nothing."""
    jobs = release_jobs()
    for required in ("validate", "test", "bump-and-tag", "artifacts", "publish-images"):
        assert required in jobs, (
            f"release.yml has no `{required}` job. Either it was renamed — in which case "
            "the assertions in this file are silently checking a different graph — or the "
            "release flow lost a stage."
        )
    assert len(jobs) >= 8, (
        f"release.yml declares only {len(jobs)} job(s): {sorted(jobs)}. The gate alone is "
        "five; a graph this small means the parse or the workflow lost most of it."
    )


def test_validate_declares_the_outputs_the_rest_of_the_graph_reads():
    """Every downstream `ref:` is one of validate's outputs. An undeclared output
    renders as an EMPTY STRING in an expression — so `ref: ''` would silently mean
    "actions/checkout's default ref", i.e. exactly the bare checkout this file
    forbids, while every textual assertion below still passed."""
    outputs = release_jobs()["validate"].get("outputs") or {}
    for name in ("version", "version_no_v", "gate_ref", "sha"):
        assert name in outputs, (
            f"release.yml's `validate` job declares no `{name}` output, so every "
            f"`needs.validate.outputs.{name}` downstream evaluates to the empty string "
            "and the checkout falls back to the default ref."
        )


# ---------------------------------------------------------------------------
# Invariant 1 + 2: no bare checkout; the gate tests one pinned commit.
# ---------------------------------------------------------------------------


def test_no_job_in_release_yml_does_a_bare_checkout():
    """A bare `actions/checkout` takes `github.ref`, which on a workflow_dispatch is
    whatever branch the operator happened to fire from and on a `release: published`
    is the tag. Mixing the two is how the gate, the tag and the tarball came from
    three different commits. Every checkout here must say which commit it means."""
    allowed = (PINNED_SHA, RELEASE_TAG, GATE_REF, "main")

    seen = 0
    offenders = []
    for job_id, job in release_jobs().items():
        if not isinstance(job, dict):
            continue
        for step in checkout_steps(job):
            seen += 1
            ref = str((step.get("with") or {}).get("ref", "")).strip()
            if not ref:
                offenders.append(f"{job_id}: `{step.get('name', '<unnamed>')}` has no `ref:`")
            elif not any(token in ref for token in allowed):
                offenders.append(
                    f"{job_id}: `{step.get('name', '<unnamed>')}` checks out {ref!r}, which "
                    f"is none of {allowed}"
                )

    # Non-vacuity: an empty `offenders` also means "found no checkout steps at all",
    # which would be a parse failure wearing a pass.
    assert seen >= 5, (
        f"only {seen} actions/checkout step(s) found across release.yml's jobs; the "
        "release flow has more than that, so this scan did not read the file it thinks "
        "it read."
    )
    assert not offenders, (
        "release.yml job(s) check out an unspecified or unexpected ref:\n"
        + "\n".join(offenders)
    )


def test_every_gate_job_checks_out_the_one_pinned_commit():
    """The five gate jobs run on five separate runners. If each resolves `main` (or
    the tag) for itself, a push landing mid-run splits the gate across two trees and
    nothing reports it. `validate` resolves the ref ONCE to a SHA and every gate job
    checks out that SHA."""
    gate = gate_jobs()
    assert len(gate) >= 5, (
        f"release.yml's `test` job needs {sorted(gate)} — fewer than the five blocking "
        "checks ci.yml's `all-tests` requires. Either the gate shrank or its `needs:` "
        "no longer names the gate jobs, and the per-job assertions here cover less than "
        "they claim."
    )
    wrong = []
    for job_id, job in gate.items():
        steps = checkout_steps(job)
        assert steps, f"gate job `{job_id}` never checks out the repository at all"
        for step in steps:
            ref = str((step.get("with") or {}).get("ref", ""))
            if PINNED_SHA not in ref:
                wrong.append(f"{job_id}: checks out {ref!r}, not {PINNED_SHA}")
    assert not wrong, (
        "gate job(s) do not check out the commit `validate` pinned, so the five gate "
        "jobs can test different trees:\n" + "\n".join(wrong)
    )


def test_bump_and_tag_is_gated_on_the_test_aggregator():
    """The whole point of the gate. Without `test` in its `needs:`, a tag lands on a
    tree nothing verified."""
    needs = needs_of(release_jobs()["bump-and-tag"])
    assert "test" in needs, (
        f"release.yml's `bump-and-tag` needs {needs}, which does not include `test`. "
        "It would tag an unverified commit."
    )


def test_bump_and_tag_refuses_to_tag_a_commit_the_gate_did_not_test():
    """`bump-and-tag` checks out `main` by branch name, not by the pinned SHA, because
    it has to PUSH to that branch. That reopens the gap on a smaller scale if main
    moves mid-run, so it must compare HEAD against validate's pinned SHA and fail."""
    text = run_text(release_jobs()["bump-and-tag"])
    assert PINNED_SHA in text and "rev-parse HEAD" in text, (
        "release.yml's `bump-and-tag` does not compare the checked-out HEAD against "
        f"`{PINNED_SHA}`. If main moves while the gate is running it would tag a commit "
        "no gate job tested."
    )


# ---------------------------------------------------------------------------
# Invariant 3 + 4: artifacts come from the created tag, after the bump.
# ---------------------------------------------------------------------------


def test_artifact_jobs_depend_on_bump_and_tag():
    """THE blocker. `needs:` is the only thing that orders jobs; without
    `bump-and-tag` in it, the tarball and the images are built in parallel with the
    version bump, from the un-bumped tree."""
    producers = artifact_producing_jobs()
    # Non-vacuity: the discovery is by shape (a `uses:` of build-images.yml, or a
    # `tar` of mirage-*.tar.gz). If it finds nothing, every assertion below is over
    # an empty set.
    assert len(producers) >= 2, (
        f"discovered only {sorted(producers)} as release-artifact producers. Expected at "
        "least the image build and the archive job — the discovery predicates "
        "(a `uses:` of build-images.yml, or a `tar` of a mirage-* archive) no longer "
        "match what release.yml does."
    )
    missing = [
        job_id
        for job_id, job in producers.items()
        if "bump-and-tag" not in needs_of(job)
    ]
    assert not missing, (
        f"release-artifact job(s) {sorted(missing)} do not `need: bump-and-tag`, so they "
        "run in PARALLEL with the version bump and build from the un-bumped tree. That is "
        "the defect that would have shipped a mirage-v1.0.0.tar.gz containing a "
        "nextflow.config reading the previous version."
    )


def test_artifact_jobs_build_from_the_created_tag():
    """Depending on `bump-and-tag` only fixes the ORDER. The jobs must also check out
    the tag it created, or they still build from whatever `github.ref` happens to be."""
    producers = artifact_producing_jobs()
    assert producers, "no artifact-producing jobs discovered"

    wrong = []
    for job_id, job in producers.items():
        if BUILD_IMAGES_USES in str(job.get("uses", "")):
            # A reusable workflow does its own checkout and sees the CALLER's
            # github.ref, so the tag has to be handed to it as an input.
            passed = str((job.get("with") or {}).get("ref", ""))
            if RELEASE_TAG not in passed:
                wrong.append(
                    f"{job_id}: calls {BUILD_IMAGES_USES} with ref={passed!r}; a reusable "
                    f"workflow checks out the CALLER's github.ref unless given "
                    f"`ref: ${{{{ {RELEASE_TAG} }}}}`"
                )
            continue
        steps = checkout_steps(job)
        assert steps, f"artifact job `{job_id}` never checks out the repository"
        for step in steps:
            ref = str((step.get("with") or {}).get("ref", ""))
            if RELEASE_TAG not in ref:
                wrong.append(f"{job_id}: checks out {ref!r}, not the released tag")
    assert not wrong, (
        "release-artifact job(s) do not build from the created tag:\n" + "\n".join(wrong)
    )


def test_build_images_honours_the_ref_it_is_given():
    """The other half of the same invariant, in the called workflow. release.yml can
    pass `ref:` all it likes; if build-images.yml's own `actions/checkout` steps ignore
    it, the images are still built from the caller's ref."""
    data = load(BUILD_IMAGES)
    triggers = data.get("on") or data.get(True) or {}
    call_inputs = ((triggers.get("workflow_call") or {}).get("inputs")) or {}
    assert "ref" in call_inputs, (
        "build-images.yml's `workflow_call` declares no `ref` input, so release.yml "
        "cannot tell it which commit to build the images from."
    )

    seen = 0
    ignoring = []
    for job_id, job in jobs_of(BUILD_IMAGES).items():
        if not isinstance(job, dict):
            continue
        for step in checkout_steps(job):
            seen += 1
            if "inputs.ref" not in str((step.get("with") or {}).get("ref", "")):
                ignoring.append(f"{job_id}: `{step.get('name', '<unnamed>')}`")
    assert seen >= 2, (
        f"found {seen} checkout step(s) in build-images.yml; both the matrix-resolving "
        "`setup` job and the `build` job check out the repo, so this scan missed one."
    )
    assert not ignoring, (
        "build-images.yml checkout step(s) ignore the `ref` input, so a release builds "
        "images from the caller's ref rather than the tag:\n" + "\n".join(ignoring)
    )


def test_jobs_needing_bump_and_tag_tolerate_it_being_skipped():
    """The trap this fix creates if left half-done.

    `bump-and-tag` carries `if: github.event_name == 'workflow_dispatch'`, so on the
    `release: published` path it is SKIPPED — and GitHub skips any job whose dependency
    was skipped. Adding `needs: bump-and-tag` without `always()` therefore DELETES the
    archive and the image publish from the release-published trigger entirely, silently,
    and the release still reports green.
    """
    offenders = []
    checked = 0
    for job_id, job in release_jobs().items():
        if not isinstance(job, dict) or "bump-and-tag" not in needs_of(job):
            continue
        checked += 1
        cond = str(job.get("if", ""))
        if "always()" not in cond:
            offenders.append(
                f"{job_id}: needs `bump-and-tag` but its `if:` has no always(), so it is "
                "SKIPPED on the release:published path where bump-and-tag is skipped"
            )
        elif "skipped" not in cond:
            offenders.append(
                f"{job_id}: has always() but never allows bump-and-tag to be 'skipped', "
                "so the release:published path is still excluded"
            )
        elif "needs.test.result == 'success'" not in cond:
            offenders.append(
                f"{job_id}: uses always() without re-asserting `needs.test.result == "
                "'success'`, so a FAILED gate no longer blocks it -- always() bypasses "
                "the needs-failure rule too"
            )
    assert checked >= 2, (
        f"only {checked} job(s) in release.yml depend on `bump-and-tag`; "
        "test_artifact_jobs_depend_on_bump_and_tag should have caught that first."
    )
    assert not offenders, "\n".join(offenders)


# ---------------------------------------------------------------------------
# Invariant 5: the gate runs what CI blocks on.
# ---------------------------------------------------------------------------

# One entry per blocking check ci.yml's `all-tests` gate requires, keyed on the
# COMMAND rather than the job name (the two workflows name their jobs differently
# and always will). The second element is what makes this non-vacuous: the same
# substring must be found in ci.yml, so a typo'd or reworded needle fails on the
# ci.yml side instead of silently matching nothing on both.
BLOCKING_CHECKS = [
    ("python unit suite", "pytest -v tests/"),
    ("DISK front-end non-skip proof", "tests/test_coarse_frontend.py"),
    ("nf-test stub suite", "nf-test test --tag stub"),
    ("ruff lint", "ruff check ."),
    ("lib/ probe (the only unit-test surface lib/*.groovy has)", "tests/lib_probe.nf"),
    ("profiles parse without the gitignored site config", "tests/check_profiles_parse.sh"),
    ("param surface consistency", "tests/check_param_consistency.py"),
    ("pipeline validation script", "tests/run_validation_tests.sh"),
    ("work-directory cleanup", "tests/cleanup_work.sh"),
    ("shipped-defaults stub run", "-profile shipped_defaults_test"),
    ("DEEPCELL token-leak guard", "tests/modules/segment_deepcell_token.nf.test"),
    ("token-guard collection assertion", "--dry-run"),
    ("Nextflow plugin provisioning", "nextflow plugin install"),
]


@pytest.mark.parametrize("label,needle", BLOCKING_CHECKS, ids=[c[0] for c in BLOCKING_CHECKS])
def test_the_release_gate_runs_every_check_ci_blocks_on(label, needle):
    """release.yml's header claims the release is gated on the same checks CI runs.

    It ran 5 of these 13. Absent were the whole nf-test stub suite, ruff, the DEEPCELL
    token-leak guard and its collection assertion, tests/lib_probe.nf,
    check_profiles_parse.sh, check_param_consistency.py, cleanup_work.sh and the
    shipped-defaults stub run — so a release could ship a broken lib/ class, a leaking
    token, an unparseable profile or a param/schema mismatch, and be green.
    """
    # Non-vacuity, and a typo detector: the needle must be a command ci.yml really
    # runs. A misspelled needle fails HERE rather than passing vacuously on both sides.
    assert needle in CI.read_text(), (
        f"{needle!r} ({label}) does not appear in ci.yml, so it is not a check CI runs "
        "and this parametrised case is asserting against a string that matches nothing."
    )

    gate_text = "\n".join(run_text(job) for job in gate_jobs().values())
    assert gate_text.strip(), "the release gate jobs contain no `run:` steps at all"
    assert needle in gate_text, (
        f"release.yml's gate does not run {needle!r} ({label}), which ci.yml's blocking "
        "`all-tests` gate requires. A release can therefore ship what CI would have "
        "rejected."
    )


def test_the_release_gate_does_not_treat_skipped_as_a_pass():
    """`skipped` is not `success`. A gate job that stops running must block the
    release rather than being waved through — the same defect ci.yml's `all-tests`
    carried until 2026-08-31."""
    text = run_text(release_jobs()["test"])
    assert "!= \"success\"" in text, (
        "release.yml's `test` aggregator does not compare gate results against "
        "\"success\"; it is not checking anything."
    )
    assert "skipped" not in text, (
        "release.yml's `test` aggregator allows a `skipped` gate job to count as a pass. "
        "The moment any gate job gains a `paths:`/`if:` filter the gate goes decorative "
        "for it."
    )
