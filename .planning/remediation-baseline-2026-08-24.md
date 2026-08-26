# Pre-remediation baseline — 2026-08-24 (captured 2026-08-25)

Captured for Task 0.1 of the architecture-review remediation plan
(`docs/superpowers/plans/2026-08-24-architecture-review-remediation.md`).
Every later phase's "this now fails / this still passes" claim is measured
against the numbers below.

## Step 1 — Branch and SHA

The brief says "Switch to `dev` and confirm." That was deliberately **not**
done: this worktree is on `remediation/arch-review-2026-08-24`, which is
branched from `dev` at its tip, and switching branches in a shared worktree
is unsafe (a second agent may be using the same checkout).

- Branch: `remediation/arch-review-2026-08-24`
- SHA: `0c14c6d`
- Verified: `git rev-parse --short dev` also prints `0c14c6d` — the worktree
  branch point is exactly `dev`'s tip, so this SHA is a valid stand-in for
  "`dev` @ baseline".

## Step 2 — Python suite

```
pytest tests/ --ignore=tests/testdata --ignore=tests/modules \
  --ignore=tests/subworkflows --ignore=tests/integration -q
```

Result: **939 passed, 9 skipped, 14 warnings in 73.61s** — matches the
brief's predicted `939 passed, 9 skipped` exactly.

(`tests/testdata/generate_complete_testdata.py` was run first, per
`CLAUDE.md`'s documented prerequisite, since some generated fixtures were
missing from this worktree's gitignored `tests/testdata/`.)

## Step 3 — Containerless stub, three runs

```
rm -rf /tmp/_nfw /tmp/_stub
nextflow -q run . -profile test -stub -w /tmp/_nfw --outdir /tmp/_stub
find /tmp/_stub -type f | wc -l
```

Nextflow: 25.04.7 (a 26.04.6 update notice printed on every run; ignored —
CLAUDE.md pins `>=25.04.0` and does not mandate 26.x).

| Run | Published file count | Exit |
|---|---|---|
| 1 | **62** | 0 |
| 2 | **62** | 0 |
| 3 | **62** | 0 |

**Discrepancy from the brief:** the brief predicts "roughly 60" and cites a
history of "60/61/60" across three runs (i.e., some run-to-run variance).
Measured here: all three runs published exactly **62** files, with no
variance between runs. Both the absolute count (62, not ~60) and the
variance behavior (none observed, vs. a historically flaky ±1) differ from
what the brief expected. This is recorded as observed, not reconciled —
later tasks (4.2, 6B.2) should treat 62 as today's true baseline count, not
60.

The manifest from the third run:

```
find /tmp/_stub -type f | sort > /tmp/_stub_manifest_before.txt
```

- **Created:** `/tmp/_stub_manifest_before.txt` — **62 lines**, one path per
  published file, sorted.
- `/tmp/_nfw` (Nextflow work dir) was deleted after the runs completed; only
  the manifest and `/tmp/_stub`'s published tree persist.

## Step 4 — ruff

```
ruff check .
```

Result: **`All checks passed!`** — no output beyond that line.

## Step 5 — nf-test

```
nf-test test --tag stub --profile test
```

nf-test 0.9.5 was available (`which nf-test` → `/opt/homebrew/bin/nf-test`).
Docker was **not running** in this environment (`docker info` failed), but
CLAUDE.md and `.github/workflows/ci.yml` both state the stub-tagged suite
needs no images, so the run was attempted anyway.

**Result: did not complete within the baseline window.** The suite ran for
roughly 14 minutes (44 `.nf.test` files under `tests/modules/` and
`tests/subworkflows/`, each spawning its own containerless
`nextflow ... -stub` subprocess) without finishing, and was killed
(`kill -9`) rather than left to run indefinitely, per instruction not to
block later tasks on it. This matches a documented gotcha in this
codebase: real (non-stub, i.e. actual `nf-test`-orchestrated) execution is
known not to run reliably on arm64/qemu.

Partial evidence captured before the kill (last 60 lines of output, i.e. a
tail window, not the full log — earlier output was not retained):

- **36 `PASSED`, 0 `FAILED`** visible in that trailing window, spanning
  suites including `WARP_SEG_QC process`, `Parameter enum validation`,
  `ADD_CYCLE subworkflow (stub)`, `ASHLAR_ADAPTER contract`,
  `countTileRows` / `requireTilesCount`, `TILED_ADAPTER fan-out gather`, and
  `ASSEMBLE_EXPORT subworkflow`.
- The last test in the window
  (`'quantify_compartments off: no whole-cell companion, contours reused in
  the nucleus slot'`, id `edf6cf23`) had **no result marker** — it was
  mid-execution when the process was killed.
- No failures were observed in the captured window, but this is not a
  completed-suite result and must not be read as "nf-test passes" — total
  pass/fail counts across all 44 files are **unknown**.

This is a legitimate baseline finding, not a gap to backfill: **later tasks
should not assume a green full nf-test run is available as a baseline
reference**, and should rely on the containerless bare-`nextflow -stub`
loop (Step 3 above) plus the tagged CI job for verification instead.

## Files/processes cleanup

- The `nf-test` process (and the `nextflow -stub` subprocess it had
  spawned) were killed; confirmed no `nf-test` or `nextflow` processes
  remained afterward (two unrelated `nextflow lsp` language-server
  processes, pre-existing from before this task, were left untouched).
- The background output-file-wait monitor was allowed to end on its own
  (it exits once the file is non-empty) — no separate teardown needed.
- `/tmp/_nfw` was removed after the three stub runs; `/tmp/_stub` and
  `/tmp/_stub_manifest_before.txt` were left in place as the artifacts
  later tasks consume.

## Environment note: `.planning/` and `.gitignore`

The task context asserted "`.planning/` is tracked." Measured fact:
`.gitignore:73` lists a bare `.planning` entry (under a "Internal AI/debug
docs (not user-facing)" heading, alongside `.claude` and `.citadel`), and
`git ls-files .planning/` returns nothing — the directory is not currently
tracked by anything in this repo's history. `git check-ignore -v` confirms
this file is ignored by that rule. Per this task's explicit instruction to
create and commit exactly this one file at this path, it was added with
`git add -f`. This mismatch between the stated environment fact and the
repo's actual `.gitignore` is flagged here for whoever owns `.gitignore`
policy — it was not otherwise acted on (no `.gitignore` edit was made).

## Summary table

| Check | Measured | Brief's prediction | Match? |
|---|---|---|---|
| Branch | `remediation/arch-review-2026-08-24` @ `0c14c6d` | `dev` @ (unspecified) | N/A — deliberate deviation, SHA identical to `dev` tip |
| pytest | 939 passed, 9 skipped | 939 passed, 9 skipped | Yes |
| stub run 1 | 62 files | ~60 | No — higher, and no run-to-run variance |
| stub run 2 | 62 files | ~60/61 | No |
| stub run 3 | 62 files | ~60 | No |
| ruff | All checks passed | (not predicted) | Clean |
| nf-test --tag stub | Did not complete; killed after ~14 min; 36/0 pass/fail in trailing window only | (not predicted numerically) | Incomplete — record as unavailable for baseline comparison |
