"""The one source of what identifies a benchmark run: sweep.yaml.

Everything downstream derives from here. Adding an axis to sweep.yaml makes it
reach the tables with no other edit, which is exactly the property that was
missing: `reg_max_image_dim` was swept over [2000, 4000, 8000] -- sweep.yaml
itself calls it the dominant runtime-and-accuracy axis of VALIS -- and was
absent from the carry, so runs differing only on it collapsed into
indistinguishable table rows.

It used to be stated three times: correctly in sweep.yaml; as 33 entries of dead
documentation in make_tables.CONFIG_COLS, imported by nothing; and as a
hardcoded 14-entry list in quality.py::run_cost_summary, which is the one that
actually ran.

WHY `axes()` EXPANDS THE PLAN RATHER THAN PARSING THE YAML'S SHAPE.

The obvious implementation walks sweep.yaml looking for `axes:` entries and
`*_grid:` blocks. It gets two things wrong, and both were found by trying it:

  * The grids do not share a shape. `scaling_grid` and `registration_param_grid`
    are flat `key: [values]`; `segmentation_grid` and `registration_method_grid`
    are nested `variant: {key: [values]}`. A shape-walker has to know which is
    which, and gets it wrong the day a third shape appears.
  * The nested grids VARY A COLUMN THAT APPEARS NOWHERE IN THEM.
    `segmentation_grid`'s variant names ARE `seg_method` values --
    build_run_plan.py pins `seg_method=<variant>` per block -- and likewise
    `registration_method` for `registration_method_grid`. A YAML walker cannot
    see those without restating build_run_plan's mapping, which is a second
    statement of the same rule and therefore the same defect one level up.

So the definition used here is the operational one: **an axis is a column whose
value differs across the runs the sweep actually emits.** build_run_plan.py is
already the single owner of how a sweep becomes runs, and it is already guarded
(benchmarks/tests/test_build_run_plan.py ties it to nextflow.config in both
directions). Asking it is strictly more faithful than re-deriving its answer.
"""

from pathlib import Path
from typing import Dict, Set, Tuple

import yaml

from benchmarks.build_run_plan import build_run_plan

# Present on every row regardless of what is swept. `run_id` and `rep` are unique
# per run rather than per configuration, so they are listed here rather than
# discovered as "varying" -- they identify the RUN, not the CONFIGURATION.
STRUCTURAL: Tuple[str, ...] = ("run_id", "config_id", "rep", "varied_axis")


def load_sweep(path: Path) -> Dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def axes(sweep: Dict) -> Set[str]:
    """Every column whose value differs across the runs this sweep emits.

    A knob that appears only in `baseline` is DECLARED, not swept: every run
    shares its value, so it identifies nothing and would only widen every table.
    An `axes:` entry with a single value is the same thing written in a
    different place.
    """
    plan = build_run_plan(sweep, repeats=1)
    if not plan:
        return set()
    # The UNION of every row's columns, not plan[0]'s. The project sweep's
    # baseline declares every swept param so the first row happens to carry them
    # all -- test_project_sweep_all_configs_share_columns enforces that, because
    # run_sweep.sh maps every column to a param -- but reading only the first row
    # makes this silently blind the moment that stops holding. Absence is itself a
    # difference, so a column present on some rows and missing on others counts as
    # varying.
    columns = {c for row in plan for c in row} - set(STRUCTURAL)
    missing = object()
    return {
        column
        for column in columns
        if len({_hashable(row.get(column, missing)) for row in plan}) > 1
    }


def _hashable(value):
    """Plan cells are scalars today; a list would raise on set() insertion and
    take the whole analysis down rather than reporting a widened axis."""
    return tuple(value) if isinstance(value, list) else value


def arm_axes(arms: Dict) -> Set[str]:
    """The same question for the REAL-SAMPLE arm experiment.

    The arms are a different experiment with a different config file, expanded
    by a different builder -- and `quality.run_cost_summary` serves BOTH: the
    Makefile's `arm-tables` target runs make_tables against the arms results
    root. Deriving identity from sweep.yaml alone silently drops every
    arms-only axis, which is how `reg_ashlar_tile` came to be hand-added to the
    old carry with a comment explaining that without it "two ashlar arms that
    differ only by tile size collapse into indistinguishable rows". That comment
    was right, and it is the reason this function exists rather than the arms
    being folded into the sweep.
    """
    from benchmarks.build_arm_plan import build_arm_plan

    plan = build_arm_plan(arms)
    if not plan:
        return set()
    columns = {c for row in plan for c in row} - set(STRUCTURAL)
    missing = object()
    return {
        column
        for column in columns
        if len({_hashable(row.get(column, missing)) for row in plan}) > 1
    }


def identity_columns(sweep: Dict, arms: Dict | None = None) -> Tuple[str, ...]:
    """Structural columns plus every axis either experiment varies.

    Sorted rather than left in plan order: the column set feeds table schemas
    that get diffed between runs of the analysis, and plan order is an artifact
    of dict insertion.

    The union is deliberate. A column an experiment does not have is filtered
    out by its caller (`if c in df.columns`), so carrying the other experiment's
    axes costs nothing, while omitting them collapses rows -- and the collapse is
    invisible, because the table renders cleanly either way.
    """
    found = axes(sweep)
    if arms is not None:
        found |= arm_axes(arms)
    return STRUCTURAL + tuple(sorted(found))
