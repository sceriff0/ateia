"""A `params/*.json` preset must be a preset that can actually run.

Two failure modes, both found in this repository:

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

The rule: if a preset sets any parameter whose name is owned by one segmentation
backend, it must also set `seg_method` to that backend.
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


def test_the_owner_map_recognises_the_names_it_is_written_for():
    assert _owner("segmentation_model_dir") == "stardist"
    assert _owner("seg_instantseg_tile_size") == "instantseg"
    assert _owner("seg_cellsam_block_size") == "cellsam"
    # Shared across backends -- owned by none.
    assert _owner("seg_gpu") is None
    assert _owner("seg_expand_distance") is None
