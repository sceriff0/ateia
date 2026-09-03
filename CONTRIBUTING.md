# Contributing to MIRAGE

Thanks for your interest. This page is the short version: how to run the checks,
where the branches go, and the handful of conventions that are enforced by tests
rather than by review. The long version — why each of those conventions exists,
and the specific ways this test suite has looked green while proving nothing — is
in [docs/developing.md](docs/developing.md), also published at
<https://mirage-pipeline.readthedocs.io/en/latest/developing/>.

## Getting set up

```bash
git clone https://github.com/sceriff0/mirage.git
cd mirage
pip install -r requirements/ci.txt
python tests/testdata/generate_complete_testdata.py
```

That last line is not optional. `tests/testdata/` is gitignored and its contents
are generated, so on a fresh clone the fixtures do not exist and test failures
look like code defects.

## Running the checks

There are four suites, and they answer different questions.

| command | what it proves |
|---|---|
| `pytest -v tests/ --ignore=tests/testdata --ignore=tests/modules --ignore=tests/subworkflows --ignore=tests/integration` | the Python scripts under `bin/`, and every static guard over the pipeline's own sources |
| `nextflow -q run . -profile test -stub -w /tmp/_nfw --outdir /tmp/_stub` | the whole workflow's channel wiring, in seconds, with no container engine |
| `nf-test test --tag stub --profile test` | per-module and per-subworkflow behaviour, stubbed |
| `ruff check . && actionlint` | Python lint and GitHub Actions lint — both blocking in CI |

`nf-test test --tag real` is the suite that runs the real tools on real bytes. It
does not run reliably on arm64 under emulation and it is not part of the merge
gate; CI runs it nightly.

## Branches

- **`main`** — the released pipeline.
- **`dev`** — where feature and fix work lands. Open pull requests against this.
- **`benchmarking`** — `dev` plus the benchmark harness under `benchmarks/`, kept
  in sync by merging `dev` into it. **The merge direction is one-way**: never
  merge `benchmarking` into `dev`, or the harness lands in the pipeline.

## Commit messages

Every commit subject starts with a [gitmoji](https://gitmoji.dev) `:shortcode:`,
in the colon form rather than the emoji character so that terminals without emoji
rendering degrade cleanly:

```
:sparkles: Add the tiled registration backend
:bug: Fix the off-by-one in the tile plan's last row
:white_check_mark: Guard the mask-series write contract
```

The ones in regular use are `:sparkles:` (feature), `:bug:` (fix),
`:white_check_mark:` (tests), `:memo:` (docs), `:recycle:` (refactor), `:fire:`
(removal), `:wrench:` (configuration), `:package:` (dependencies), `:art:`
(formatting).

## The rule that matters most

**Watch every new guard fail before you trust it.**

This is not general advice; it is the specific lesson this repository keeps
relearning. Guards here have passed while checking nothing because a glob excluded
the interesting files, because a negative assertion could not have fired, because
an allowlist entry's stated reason had counterexamples, and — six separate times in
one day — because a *comment* naming the thing under test satisfied a text match
while the real command had been deleted.

So: after writing a guard, break the thing it guards, run it, and see it go red.
Then restore and see it go green. Every guard commit in this repository's history
that was worth having describes that step in its message.

## Opening a pull request

1. Branch from `dev`.
2. Run the four suites above. `ruff check .` and `actionlint` are blocking in CI —
   run them before pushing rather than after.
3. If you added a test file for Nextflow, tag it `stub`, or CI's blocking gate
   never collects it.
4. If you added a test fixture, add its producer to
   `tests/testdata/generate_complete_testdata.py` and its path to `.gitignore` —
   a hand-written fixture exists on exactly one machine.
5. If you added tests, re-ratchet `tests/collected_floor.txt` to the new collected
   count.

Read [docs/developing.md](docs/developing.md) before your first pull request. It
is short, and most of it is things that cannot be inferred from the code.
