"""A `params/*.json` preset must be a preset that can actually run.

Three failure modes, all found in this repository:

1. **A key the schema does not know.** `-params-file` values go through
   nf-schema's validator, and an unknown key is either rejected or silently
   inert depending on the strictness setting. Either way the preset is lying
   about what it configures.

2. **A backend's parameters without the backend.** `params/full_pipeline.json`
   carried the whole StarDist block -- `seg_pmin`, `seg_n_tiles_x`,
   `segmentation_model`, ... -- and set `seg_method: "stardist"` with
   `segmentation_model_dir: null`, which cannot run (bin/segment.py raises
   FileNotFoundError, and the shipped model name is not a StarDist built-in).
   `params/postprocessing_only.json` had shipped the mirror image: the StarDist
   block with NO seg_method at all, which read as a configuration choice and was
   inert. Ruling R5 for 1.0.0 switches full_pipeline.json to `instantseg`.

3. **Site sizing baked into a preset.** `full_pipeline.json`,
   `preprocessing_only.json`, `registration_only.json` and
   `postprocessing_only.json` all hardcoded IEO's `max_cpus`/`max_memory`/
   `max_time` (`128`/`700.GB`/`240.h`). `-params-file` overrides `-c`
   (Nextflow's own precedence), so copying one of these presets onto another
   cluster and layering `-c site.config` never resizes it -- the preset's
   ceiling silently wins. Ruling R4 for 1.0.0 makes `conf/site.config.template`
   the one owner of per-site sizing; a production preset must leave
   `max_cpus`/`max_memory`/`max_time` for the site config to supply.

The rule for (2): if a preset sets any parameter whose name is owned by one
segmentation backend, it must also set `seg_method` to that backend.

The rule for (3): no preset may set `max_cpus`, `max_memory` or `max_time`,
except an explicitly exempted self-contained test preset.
"""

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PRESETS = sorted((REPO / "params").glob("*.json"))

# Parameter-name prefixes owned by one backend, and the seg_method value that
# activates them. Read off nextflow.config's segmentation block.
BACKEND_PREFIXES = {
    "stardist": (
        "seg_pmin",
        "seg_pmax",
        "seg_prob_thresh",
        "seg_n_tiles_x",
        "seg_n_tiles_y",
        "segmentation_model",
        "segmentation_model_dir",
    ),
    "instantseg": ("seg_instantseg_", "instanseg_model_dir"),
    "cellsam": ("seg_cellsam_", "cellsam_model_path"),
}

# R4: conf/site.config.template is THE owner of per-site sizing. A preset that
# sets any of these bakes one site's numbers into a file `-params-file` loads
# ahead of `-c site.config`, silently defeating whatever ceiling the site config
# supplies.
SITE_SIZING_KEYS = ("max_cpus", "max_memory", "max_time")

# Presets exempt from the site-sizing rule, with why. Each is verified below to
# still set at least one site-sizing key, so a stale entry cannot quietly cover
# a real production preset that started hardcoding a ceiling again.
SITE_SIZING_EXEMPT = {
    "test.json": (
        "a self-contained CI/test preset that intentionally pins tiny ceilings "
        "(2 cpus / 6.GB / 6.h) so the suite does not depend on -c site.config; "
        "not one of the four production presets R4 targets"
    ),
}


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


def _schema_seg_method_enum():
    """`seg_method`'s enum in nextflow_schema.json -- the set of real backends."""
    schema = json.loads((REPO / "nextflow_schema.json").read_text())

    def walk(node):
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict) and "seg_method" in props:
                return props["seg_method"].get("enum")
            for value in node.values():
                found = walk(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = walk(value)
                if found is not None:
                    return found
        return None

    enum = walk(schema)
    assert enum, "nextflow_schema.json's seg_method has no enum -- the walk is broken"
    return set(enum)


def _owner(name):
    for backend, prefixes in BACKEND_PREFIXES.items():
        for prefix in prefixes:
            if name == prefix or name.startswith(prefix):
                return backend
    return None


def test_there_are_presets_to_check():
    assert len(PRESETS) >= 5, f"only {[p.name for p in PRESETS]} -- the glob is wrong"


@pytest.mark.parametrize("preset", PRESETS, ids=[p.name for p in PRESETS])
def test_every_preset_key_is_a_real_parameter(preset):
    known = _schema_params()
    data = json.loads(preset.read_text())
    unknown = sorted(k for k in data if not k.startswith("_comment") and k not in known)
    assert not unknown, (
        f"params/{preset.name} sets {unknown}, which nextflow_schema.json does not "
        "declare. A preset key the schema does not know configures nothing."
    )


@pytest.mark.parametrize("preset", PRESETS, ids=[p.name for p in PRESETS])
def test_a_preset_that_configures_a_backend_selects_it(preset):
    data = json.loads(preset.read_text())
    selected = data.get("seg_method")
    owners = {_owner(k) for k in data if not k.startswith("_comment")}
    owners.discard(None)
    stranded = sorted(o for o in owners if o != selected)
    assert not stranded, (
        f"params/{preset.name} sets parameters owned by {stranded} but its "
        f"seg_method is {selected!r}. Those values are silently discarded: the "
        "backend that reads them is never selected."
    )


@pytest.mark.parametrize("preset", PRESETS, ids=[p.name for p in PRESETS])
def test_a_preset_does_not_bake_in_site_sizing(preset):
    if preset.name in SITE_SIZING_EXEMPT:
        pytest.skip(SITE_SIZING_EXEMPT[preset.name])
    data = json.loads(preset.read_text())
    baked = sorted(k for k in SITE_SIZING_KEYS if k in data)
    assert not baked, (
        f"params/{preset.name} sets {baked}, which R4 reserves for "
        "conf/site.config.template. A -params-file value overrides -c, so these "
        "would silently defeat any site.config layered on top of this preset."
    )


def test_the_site_sizing_exemption_is_not_stale():
    """An exemption for a preset that no longer sets any site-sizing key is a
    licence nobody is using and the next reader will trust."""
    for name, reason in SITE_SIZING_EXEMPT.items():
        assert reason.strip(), f"{name} has an empty reason"
        preset = REPO / "params" / name
        assert preset.exists(), f"SITE_SIZING_EXEMPT names {name}, which does not exist"
        data = json.loads(preset.read_text())
        assert any(k in data for k in SITE_SIZING_KEYS), (
            f"SITE_SIZING_EXEMPT names {name}, which no longer sets any of "
            f"{SITE_SIZING_KEYS} -- the exemption is dead"
        )


def test_backend_prefixes_covers_exactly_the_schema_backends():
    """A fourth backend added to the schema's enum without a BACKEND_PREFIXES
    entry would silently exempt every one of its parameters from the coherence
    rule above; a stale entry (a backend removed from the schema) would check a
    backend that no preset can select. Both directions, so this can't drift."""
    assert set(BACKEND_PREFIXES) == _schema_seg_method_enum()


def test_the_owner_map_recognises_the_names_it_is_written_for():
    assert _owner("segmentation_model_dir") == "stardist"
    assert _owner("seg_instantseg_tile_size") == "instantseg"
    assert _owner("seg_cellsam_block_size") == "cellsam"
    # Shared across backends -- owned by none.
    assert _owner("seg_gpu") is None
    assert _owner("seg_expand_distance") is None
