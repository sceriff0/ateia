/*
 * COMPILE_PANEL - validate panel.yaml and emit the frozen model_config.json.
 * Reads NO cell data (--validate-only); in-pipeline provenance + fail-fast.
 */
process COMPILE_PANEL {
    tag "panel"
    label 'process_single'

    container "bolt3x/attend_image_analysis:quantification_gpu"

    input:
    path panel_yaml

    output:
    path "model_config.json", emit: model_config
    path "spec_report.html" , emit: report
    path "versions.yml"     , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    """
    compile_panel.py \\
        --panel ${panel_yaml} \\
        --out model_config.json \\
        --report spec_report.html \\
        --validate-only --accept-all \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        pyyaml: \$(python -c "import yaml; print(yaml.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    """
    echo '{"feasible_set":[],"constraints":{"never":[],"enforce":[],"audit":[],"requires":[]},"palette":{},"phenotypes":[],"markers":{}}' > model_config.json
    touch spec_report.html
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        pyyaml: stub
    END_VERSIONS
    """
}
