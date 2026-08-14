from utils.phenotyping.constraints import split_constraints
from utils.phenotyping.feasible import enumerate_feasible
from utils.phenotyping.model_config import build_model_config, write_spec_report_html
from utils.phenotyping.palette import RESERVED, build_palette
from utils.phenotyping.panel_schema import parse_panel
from utils.phenotyping.references import resolve_references


def _cfg(tmp_path):
    p = parse_panel("tests/testdata/panel_min.yaml")
    F = enumerate_feasible(p)
    split = split_constraints(p)
    refs, _ = resolve_references(p, split)
    pal = build_palette([ph for ph in p.phenotypes])
    return p, build_model_config(p, F, split, refs, pal,
                                 alpha_target=0.05, min_calibration=50, spec_version="panel@sha256:test")


def test_palette_has_reserved_and_distinct_phenotype_colors():
    pal = build_palette(["Tumour", "Immune", "T_cell"])
    for k, v in RESERVED.items():
        assert pal[k] == v
    cols = [tuple(pal[n]) for n in ["Tumour", "Immune", "T_cell"]]
    assert len(set(cols)) == 3


def test_model_config_shape(tmp_path):
    _, cfg = _cfg(tmp_path)
    assert set(cfg) >= {"markers", "phenotypes", "feasible_set", "constraints", "runtime", "palette", "spec_version"}
    assert cfg["markers"]["CD3"]["compartment"] == "Cytoplasm"
    assert cfg["markers"]["CD3"]["statistic"] == "Mean"
    assert cfg["runtime"]["alpha_target"] == 0.05
    # never pair present in constraint table
    never_pairs = {tuple(sorted(c["markers"])) for c in cfg["constraints"]["never"]}
    assert ("CD45", "PanCK") in never_pairs
    # is_leaf: CD8_T is a leaf, Immune is not
    leaves = {ph["name"]: ph["is_leaf"] for ph in cfg["phenotypes"]}
    assert leaves["CD8_T"] is True and leaves["Immune"] is False


def test_spec_report_writes_html(tmp_path):
    _, cfg = _cfg(tmp_path)
    out = tmp_path / "spec_report.html"
    write_spec_report_html(cfg, [], ["a warning"], str(out))
    text = out.read_text()
    assert "<html" in text.lower() and "a warning" in text


def test_runtime_carries_ambiguous_fallback():
    from utils.phenotyping.model_config import build_model_config
    from utils.phenotyping.panel_schema import parse_panel

    panel = parse_panel(
        {"markers": {}, "phenotypes": {}, "settings": {"ambiguous_fallback": "none"}}
    )
    cfg = build_model_config(
        panel, [], {"never": [], "enforce": [], "audit": [], "requires": []}, {}, {},
        alpha_target=0.05, min_calibration=50, spec_version="test",
    )
    assert cfg["runtime"]["ambiguous_fallback"] == "none"


def test_orderings_are_derived_not_literal():
    from utils.phenotyping.model_config import build_model_config
    from utils.phenotyping.panel_schema import parse_panel

    panel = parse_panel({
        "markers": {
            "CD3": {"role": "lineage", "compartment": "cell", "statistic": "Median"},
            "KI67": {"role": "state", "compartment": "nuclear", "statistic": "Median"},
        },
        "phenotypes": {"T_cell": {"CD3": "+"}},
    })
    feasible = [{"pattern": {"CD3": 1}, "phenotype": "T_cell"},
                {"pattern": {"CD3": 0}, "phenotype": "Unclassified"}]
    cfg = build_model_config(
        panel, feasible, {"never": [], "enforce": [], "audit": [], "requires": []},
        {"CD3": {"neg_source": {}, "pos_source": {}},
         "KI67": {"neg_source": {}, "pos_source": {}}},
        {}, alpha_target=0.05, min_calibration=50, spec_version="test",
    )
    assert cfg["phenotype_order"] == ["T_cell", "Unclassified"]
    # lineage_order IS lineage_markers -- the same object, never a second sort.
    assert cfg["lineage_order"] == cfg["lineage_markers"] == ["CD3"]
    assert cfg["outcome_names"] == ["Ambiguous", "Conflict", "Artefact", "Unclassified"]
