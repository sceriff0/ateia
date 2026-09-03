"""`docs/parameters.md` must document exactly the schema's parameter surface.

This page is hand-maintained -- its own standfirst says so -- and it is the page
readers use to decide what to set. Two ways it rots, both silent:

* a parameter is added to nextflow.config and the schema and never documented, so
  it is invisible to everyone who is not reading Groovy; or
* a parameter is removed and its row survives, so the page advertises a flag that
  is now rejected at launch.

The page was exactly in sync on 2026-09-03 -- 89 documented rows plus the two
`config_profile_*` identity strings a site profile sets, against the schema's 91.
That was a coincidence, not a property. This makes it a property.

The parse is deliberately narrow: a parameter is "documented" when it is the first
cell of a Markdown table row, in backticks -- the shape every parameter table on
that page already uses. Prose mentioning a name does not count, because prose
mentions names in passing all the time.
"""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "parameters.md"

# Parameters a RUN never sets: nf-core identity strings a site profile carries.
# Verified below to still exist in the schema, so a stale entry cannot hide a real
# undocumented parameter.
NOT_RUN_FACING = {
    "config_profile_name": "nf-core identity string, set by a site profile, not by a run",
    "config_profile_description": "nf-core identity string, set by a site profile, not by a run",
}

_ROW = re.compile(r"^\|\s*`([A-Za-z_][A-Za-z0-9_]*)`\s*\|", re.M)


def _schema_params():
    schema = json.loads((REPO / "nextflow_schema.json").read_text())
    names = set()

    def walk(node):
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                names.update(k for k, v in props.items() if isinstance(v, dict))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    assert len(names) > 50, (
        f"only {len(names)} schema params found -- the walk is broken"
    )
    return names


def _documented():
    rows = set(_ROW.findall(DOC.read_text()))
    assert len(rows) > 50, (
        f"only {len(rows)} parameter rows parsed out of docs/parameters.md -- the row "
        "shape changed and every check below would pass vacuously"
    )
    return rows


def test_every_schema_parameter_has_a_row():
    missing = sorted(_schema_params() - _documented() - set(NOT_RUN_FACING))
    assert not missing, (
        "nextflow_schema.json declares parameters docs/parameters.md does not "
        "document: " + ", ".join(missing) + ". The page is hand-maintained; add a "
        "row in the group the parameter belongs to."
    )


def test_no_row_documents_a_parameter_that_no_longer_exists():
    stale = sorted(_documented() - _schema_params())
    assert not stale, (
        "docs/parameters.md documents parameters the schema no longer declares: "
        + ", ".join(stale)
        + ". A run passing one of these is rejected at launch, so the page is "
        "advertising a flag that does not work."
    )


def test_the_exemptions_are_not_stale():
    schema = _schema_params()
    for name, reason in NOT_RUN_FACING.items():
        assert reason.strip(), f"{name} has an empty reason"
        assert name in schema, (
            f"NOT_RUN_FACING names {name}, which the schema no longer declares -- "
            "the exemption is dead and would hide the next undocumented parameter"
        )


def test_the_page_still_says_it_is_hand_maintained():
    """The guard checks the NAME SET only -- defaults and descriptions are still
    a human's job, and the page must keep telling the reader that."""
    text = DOC.read_text()
    assert "not</em> machine-checked" in text or "not* machine-checked" in text, (
        "docs/parameters.md no longer warns that its defaults and descriptions are "
        "hand-maintained. This guard checks the parameter NAMES only."
    )
