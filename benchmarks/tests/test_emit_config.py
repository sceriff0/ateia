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


def test_known_process_emits_live_block_with_continuous_gib(tmp_path):
    out = tmp_path / "k.config"
    emit_config.write_optimized_config(
        {"SEGMENT": {"slope": 7.0, "intercept": 8.0, "sigma": 4.0, "r2": 0.97, "n": 5}}, out)
    text = out.read_text()
    assert "    withName: 'SEGMENT'" in text          # live (uncommented) block
    # continuous GiB, matching the model fit on bytes / 2**30 (not floored >> 30)
    assert "merged_file.size() / (1024 ** 3)" in text
    assert ">> 30" not in text                        # no integer-floor GiB
    assert "?: 1" not in text                         # no 1 GiB minimum floor


def test_unknown_process_emitted_commented_not_invalid_var(tmp_path):
    out = tmp_path / "u.config"
    emit_config.write_optimized_config(
        {"QUANTIFY": {"slope": 1.0, "intercept": 2.0, "sigma": 0.5, "r2": 0.9, "n": 5}}, out)
    text = out.read_text()
    assert "file_gb" not in text                      # no invalid placeholder variable
    assert "total_gb" not in text
    assert "// withName: 'QUANTIFY'" in text           # emitted but inert (commented)
    assert "    withName: 'QUANTIFY'" not in text      # NOT an active block


def test_merge_and_pyramid_is_not_emitted_with_invalid_total_gb(tmp_path):
    out = tmp_path / "m.config"
    emit_config.write_optimized_config(
        {"MERGE_AND_PYRAMID": {"slope": 1.0, "intercept": 2.0, "sigma": 0.5, "r2": 0.9, "n": 5}}, out)
    text = out.read_text()
    assert "total_gb" not in text
    assert "// withName: 'MERGE_AND_PYRAMID'" in text  # commented, awaiting a real expr
