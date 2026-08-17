# `modules/nf-core/basicpy` — vendored, and the two things it does differently

This directory is nf-core/modules' `basicpy` module, copied verbatim. `main.nf` and
`meta.yml` are byte-for-byte upstream; nothing here is patched.

Two tests hold that, and it is worth knowing which does what:

* `tests/test_basicpy_module_is_vendored_unmodified.py` pins the **sha256 of each file**,
  so any edit at all fails — including one this document does not anticipate. It also
  spells out the two lines mirage builds on (the `/opt/main.py` invocation, and the
  `dfp`-before-`ffp` output tuple) so a digest mismatch can be triaged without refetching.
  It pins *unchanged since vendoring*; it cannot pin *identical to upstream today*,
  because that needs the network and a guard that skips when offline is worse than none.
* `tests/test_basicpy_defaults_are_deliberate.py` pins the four properties discussed
  below — container tag, conda refusal in both blocks, the version literal and its topic
  emit — plus the decision to pass no arguments. Those fail with messages that explain
  themselves; the digest does not.

It is vendored rather than installed with `nf-core modules install` because mirage has no
`modules.json` and installs nothing else from nf-core — one directory copied by hand is a
smaller surface than adopting the tooling for a single module.

Two behaviours differ from every process in `modules/local/`, and both are load-bearing.

## 1. It refuses `-profile conda` / `-profile mamba` by design

Both its `script:` and its `stub:` block open with

```groovy
if (workflow.profile.tokenize(',').intersect(['conda', 'mamba']).size() >= 1) {
    error "Basicpy module does not support Conda. Please use Docker / Singularity instead."
}
```

mirage ships no conda profile, so this cannot fire today. It is worth knowing that adding
one would make the preprocessing step fail at process instantiation rather than at run
time, and that `-stub` does not exempt it — the guard is in the stub block too.

## 2. Its version emit is a hardcoded literal, and mirage does not collect it

```groovy
tuple val("${task.process}"), val('basicpy'), val("1.2.0"), emit: versions_basicpy, topic: versions
// WARN: Version information not provided by tool on CLI. Please update this string when bumping
```

Two separate consequences:

* The string `1.2.0` is maintained by hand upstream. It is **not** read from the running
  container, so it does not report the version that actually ran — and the container this
  module pulls is tagged `1.2.0-patch5`, which the literal does not say.
* It is emitted on a **versions topic channel** as a 3-tuple, not as a `versions.yml`
  file. mirage's QC aggregation (`subworkflows/local/final_qc.nf` → `GENERATE_QC_REPORT`)
  collects `path versions.yml` artifacts, so this emit is not compatible with it and
  **BASICPY does not appear in mirage's version report**.

Wiring the topic into that report would mean either patching the vendored module (giving
up the "unmodified" property this directory's guard asserts) or publishing a version
string that upstream itself warns is unreliable. Neither is worth it, so the gap is
recorded here and in `docs/basic_illumination.md` instead. `TILE_FOR_BASIC` and
`APPLY_PROFILES` — the two mirage processes on either side of it — do emit `versions.yml`
normally, so the step is not versionless, only the vendored middle of it is.

## Arguments: none. That is a decision.

`conf/modules.config`'s `withName: 'BASICPY'` block sets `ext.args = ''`. The module runs
at **upstream defaults**, and mirage's previous in-process parameters
(`BaSiC(get_darkfield=True, smoothness_flatfield=1)`) are deliberately **not**
reproduced. `tests/test_basicpy_defaults_are_deliberate.py` fails if a flag appears.

| setting | mcmicro CLI default (what runs) | mirage's old in-process call |
|---|---|---|
| `smoothness_flatfield` | 2.5 | 1 |
| `smoothness_darkfield` | 5.0 | 1.0 (basicpy default) |
| `sparse_cost_darkfield` | 0.01 | 0.01 |
| `max_reweight_iterations` | 20 | 10 (basicpy default) |
| `fitting_mode` | `ladmap` | `ladmap` |
| `get_darkfield` | **False** | **True** |
| `sort_intensity` | False | False |
| `device` | `cpu` | `cpu` |
| autotune | OFF | OFF |

Note that the CLI's defaults are not basicpy's own: basicpy 1.2.0 declares
`smoothness_flatfield = 1.0`, `smoothness_darkfield = 1.0` and
`max_reweight_iterations = 10`. Three of the rows above therefore differ from mirage's old
call only because mcmicro's wrapper chose different numbers.

**The one row that is not a tuning difference is `get_darkfield`.** At False, basicpy never
updates its `D_R`/`D_Z` terms from their zero initialisation, so no additive offset is
estimated and none is removed — only the multiplicative flatfield is divided out. `fit()`
still runs `self.darkfield = skimage_resize(D, images.shape[1:])`, so the module still
writes a `*-dfp.ome.tif`: **present, correctly shaped, and all zeros.**
`bin/apply_basic_profiles.py` detects that and logs the correction as flatfield-only; it
also accepts an absent darkfield file and treats it as zero. Neither case is assumed.

`--no_autotune` is the trap to know about. It is declared `action="store_false"` with
`default=True` and gated by `if not args.no_autotune:`, so the flag's name is **inverted**:
*not* passing it skips autotune (which is what we want), and passing it enables autotune.
mirage does not pass it.

One upstream setting cannot be reached from the CLI at all: the container hardcodes
`resize_mode='skimage_dask'` where basicpy's own default is `'jax'`. Profiles from this
path are therefore not bit-identical to what an in-process `BaSiC(...)` would have fitted,
independently of every flag above.
