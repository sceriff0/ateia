from benchmarks.analysis.lib import emit_config


def test_memory_closure_formats_additive_sigma_buffer():
    model = {"slope": 7.0, "intercept": 8.0, "sigma": 4.0, "r2": 0.97, "n": 5}
    line = emit_config.memory_closure("SEGMENT", model, input_expr="file_gb")
    # check_max(( <expr>*slope + intercept + sigma*task.attempt ).GB, 'memory')
    assert "withName: 'SEGMENT'" in line
    assert "file_gb * 7.0" in line
    assert "+ 8.0" in line
    assert "+ 4.0 * task.attempt" in line
    assert "check_max(" in line and ".GB, 'memory'" in line


def test_write_optimized_config_emits_header_and_blocks(tmp_path):
    models = {
        "SEGMENT": {"slope": 7.0, "intercept": 8.0, "sigma": 4.0, "r2": 0.97, "n": 5},
        "CONVERT_IMAGE": {"slope": 1.0, "intercept": 2.0, "sigma": 0.5, "r2": 0.9, "n": 5},
    }
    out = tmp_path / "modules.optimized.config"
    emit_config.write_optimized_config(models, out)
    text = out.read_text()
    assert text.lstrip().startswith("//")  # provenance header comment
    assert "withName: 'SEGMENT'" in text
    assert "withName: 'CONVERT_IMAGE'" in text
    # processes with no known input expr fall back to a documented default
    assert "r2=0.97" in text  # fit quality annotated as a comment


def test_low_confidence_fit_is_flagged(tmp_path):
    models = {"FOO": {"slope": 1.0, "intercept": 1.0, "sigma": 1.0, "r2": 0.2, "n": 4}}
    out = tmp_path / "c.config"
    emit_config.write_optimized_config(models, out)
    assert "LOW CONFIDENCE" in out.read_text()
