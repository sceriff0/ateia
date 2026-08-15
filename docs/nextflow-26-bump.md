# Follow-up: bump `nextflowVersion` and `nf-schema`

**Status: NOT performed in this branch (`fix/red-build-bad-mesh`), by ruling.**
This document is the write-up requested by Task 5 — it exists so the
follow-up survives even though it lives in a gitignored `CLAUDE.md`-adjacent
context. It is tracked (not gitignored) and should be treated as the starting
point for the actual bump PR.

## 1. The diff

Current `nextflow.config` (read directly, lines 11-22 and 29-36):

```groovy
plugins {
    id 'nf-schema@2.5.1'
}
...
manifest {
    ...
    nextflowVersion = '>=25.04.0'
    ...
}
```

The bump would be:

```diff
 plugins {
-    id 'nf-schema@2.5.1'
+    id 'nf-schema@2.8.0'
 }
 ...
 manifest {
     ...
-    nextflowVersion = '>=25.04.0'
+    nextflowVersion = '>=26.04.0'
     ...
 }
```

`2.8.0` is the newest nf-schema release observed on this machine
(`~/.nextflow/plugins/nf-schema-2.8.0` exists locally alongside the pinned
`2.5.1`) — it is **not** the outcome of testing this pipeline against it, only
a data point that a newer release exists. The floor `>=26.04.0` is chosen
deliberately over the lower `>=25.10.0` that `nf-schema` 2.6.0+ merely
requires: `26.04.0` is the only engine line this pipeline has actually been
run against end-to-end (Task 4 verified `NXF_VER=26.04.6` specifically).
Picking `25.10.x` as the floor would declare support for an engine line that
was never exercised.

Whoever performs the bump should re-derive the exact nf-schema version from
its own changelog at bump time rather than trust `2.8.0` blindly — the point
of this section is the *shape* of the diff and the reasoning for coupling the
two lines, not a frozen version number.

## 2. Why engine and plugin must move in one commit

`nf-schema` 2.6.0+ declare `requires >=25.10` in their own plugin manifest.
The pipeline's `nextflowVersion` and its pinned `nf-schema` version are
already coupled today — the comment at `nextflow.config:11-15` explains that
`2.5.1` was chosen specifically *because* it is "the newest nf-schema whose
`requires` (>=25.04.0) matches this pipeline's own manifest.nextflowVersion."
Bumping only `nextflowVersion` and leaving `nf-schema@2.5.1` pinned changes
nothing (2.5.1 still works on a newer engine). Bumping only the plugin id
without raising `nextflowVersion` produces a manifest that *lies* about its
own floor — a `>=25.04.0` claim would let a user launch on an engine `nf-schema`
2.6.0+ refuses to load. The two lines are one change or none.

## 3. Hazards — all three are concrete, not theoretical

### 3a. nf-schema 2.7.0 tightened empty-value validation

Starting at 2.7.0, `nf-schema` began validating empty strings, empty lists,
and `false` in places earlier versions silently skipped. Any parameter whose
schema entry has an unstated implicit assumption that "empty means unset, so
skip validation" should be expected to start failing loudly post-bump. This
needs its own sweep of `nextflow_schema.json` against the parameter defaults
in `nextflow.config`, and its own test run — it is not something this
write-up can pre-verify without performing the bump.

### 3b. MEASURED: explicit boolean CLI flags are rejected on 26.04.6

During Task 4's verification, an explicitly-passed `--enable_trace true` on
the command line was **rejected** on `NXF_VER=26.04.6` with:

```
Value is [string] but should be [boolean]
```

`NXF_VER=25.04.7` coerced the same string silently. This is a real
regression surface, not a guess — 25.04.7 users have been passing string
`"true"`/`"false"` on the CLI without incident, and that stops working.

**Params confirmed boolean-typed** (`type: boolean` in `nextflow_schema.json`,
17 total): `allow_auto_reference`, `debug_channels`, `dry_run`, `embed_masks`,
`enable_trace`, `expanded_quantification`, `preproc_skip_nuclear`,
`quantify_compartments`, `seg_cellsam_use_wsi`, `seg_gpu`,
`skip_final_qc_report`, `skip_postprocessing_qc`, `skip_preprocess_qc`,
`skip_preprocessing`, `skip_registration_qc`, `skip_spatialdata_export`,
`spatialdata_include_image`.

**Grep sweep result** — every `--<boolean-param> true|false` on a literal
command line across `docs/`, `.github/workflows/`, `params/*.json`,
`conf/*.config`, `tests/`, `README.md`, `CHANGELOG.md`:

- `params/*.json` — **not at risk**. Every boolean there is a real JSON
  literal (`"enable_trace": true`, not a quoted string), loaded via
  `-params-file`, which is a different code path from CLI-string parsing and
  was not the one that broke in Task 4's measurement.
- `.github/workflows/*.yml` — no inline `--<bool> true|false` invocations
  found directly in workflow YAML.

**Actual pre-bump work — CLI invocations of a boolean param as a literal
string, found by the grep sweep:**

| File | Line(s) | Flag | Executed by CI? |
|---|---|---|---|
| `tests/run_validation_tests.sh` | 63 | `--dry_run true` | **Yes** — `.github/workflows/ci.yml:185` and `.github/workflows/release.yml:129` both `bash tests/run_validation_tests.sh` directly |
| `README.md` | 90 | `--dry_run true` | copy-paste example only |
| `docs/usage.md` | 82, 132 | `--dry_run true` | copy-paste examples only |
| `tests/test_validation.md` | 365 | `--dry_run true` | doc mirror of the script above |
| `docs/installation.md` | 140, 141, 144 | `--seg_gpu true` / `--seg_gpu false` | copy-paste examples only |
| `docs/resources.md` | 294, 311 | `--seg_gpu true` / `--seg_gpu false` | prose examples only |
| `docs/usage.md` | 266, 470 | `--seg_gpu false` | prose examples only |
| `docs/add_cycle.md` | 13 | `--embed_masks true` | copy-paste example only |
| `docs/parameters.md` | 205, 302, 306, 308 | `--expanded_quantification true`, `--embed_masks true` | prose examples only |
| `docs/outputs.md` | 45, 94 | `--allow_auto_reference true`, `--quantify_compartments false` | prose examples only |
| `CHANGELOG.md` | 175-177, 185, 536 | `--embed_masks true`, `--skip_final_qc_report=false`, `--enable_trace=true` | historical, not executed |

**The one item that actually breaks CI on the day of the bump** is
`tests/run_validation_tests.sh:63` — it is invoked directly by both
`ci.yml` and `release.yml`, not just documented. It must be changed (e.g. to
a `-params-file` with a real JSON boolean, or to whatever CLI spelling the
bumped nf-schema still coerces, if any) as part of the same PR that performs
the bump, or that PR's own CI goes red immediately. Everything else in the
table is a documentation correctness problem, not a CI-breaking one, but all
of it teaches users a spelling that stops working.

### 3c. MEASURED: `NXF_SYNTAX_PARSER=v2` on 25.04.7 is not a faithful preview of 26

Once the `.nf` sources were made strict-parser-clean (Task 4), running them
under `NXF_SYNTAX_PARSER=v2 nextflow ...` **on the local 25.04.7 engine**
does not reproduce a clean 26 run: it dies with
`DuplicateModuleFunctionException` on any `.nf` file `include`d by two
different scripts — here, `preprocess.nf`, included from both `mirage.nf`
and `add_cycle.nf`. Real `NXF_VER=26.04.6` runs this exact tree green
(`completed=2 failed=0` in the isolated repro; `completed=30 failed=0` on the
full stub suite). This is a backport defect in 25.04.7's v2 opt-in, not a
real violation in the pipeline.

**Practical consequence, recorded so nobody re-chases this:** the
`NXF_SYNTAX_PARSER=v2` opt-in on an old engine is a **violation finder**, not
a **migration oracle**. It is useful for surfacing syntax the strict parser
will reject, but a clean run under it does not certify the pipeline for 26,
and a failure under it does not necessarily mean the pipeline is broken on
26 — both directions must be checked against a real 26 engine before
trusting the result.

## 4. What is already verified (Task 4, this branch)

The pipeline runs on `NXF_VER=26.04.6` with the **default** (strict) parser:

- Exit 0
- `completed=30 failed=0 cached=0`
- 62 published files
- The normalized published-path set is **identical** to the Nextflow 25
  baseline (empty diff)

This was independently re-confirmed by the controller, not just claimed by
the implementer.

## 5. What CI would need (not edited here — owned by Tasks 1/2, guarded by two tests)

`nextflow-stub`'s matrix in `.github/workflows/ci.yml` is currently:

```yaml
matrix:
  nextflow-version: ['25.04.0', 'latest-everything']
```

The low end of that matrix exists to pin CI to the manifest's declared
floor, so that `nextflowVersion` is not just documentation but an actually
tested lower bound. Once `nextflowVersion` moves to `>=26.04.0`, `'25.04.0'`
becomes a leg that tests an engine the manifest itself says is unsupported —
it should become `'26.04.0'` (or whatever floor is actually chosen in step
1) so the matrix keeps testing "the declared minimum" rather than a stale
number. `'latest-everything'` needs no change; it already tracks whatever is
current. This edit is explicitly **not made here** — `.github/workflows/ci.yml`
is owned by Tasks 1 and 2 of this plan and is guarded by two tests, so the
change belongs in the same PR that performs the bump, not in this
documentation-only task.

## Recap: why this isn't happening in `fix/red-build-bad-mesh`

The local default Nextflow engine in this environment is 25.04.7.
`NXF_VER=26.04.6` was used only for one-off verification runs, not as the
project's default toolchain — upgrading the shared toolchain is outside this
worktree's scope. Bumping `nextflowVersion` here would make a claim
(">=26.04.0 works") that this branch cannot fully re-verify on every command
(only the specific runs already logged in Task 4's report), and the nf-schema
2.7.0 empty-value hazard above means the bump is very likely to surface new,
unrelated schema failures that deserve their own change and their own
verification pass — not to be silently folded into a build-parser migration.
