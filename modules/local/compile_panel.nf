/*
 * COMPILE_PANEL - validate panel.yaml and emit the frozen model_config.json.
 * Reads NO cell data (--validate-only); in-pipeline provenance + fail-fast.
 */
process COMPILE_PANEL {
    tag "panel"

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

    ${ProcessEnvelope.versions(task.process, ['yaml'])}
    """

    stub:
    """
    echo '{"feasible_set":[],"constraints":{"never":[],"enforce":[],"audit":[],"requires":[]},"palette":{},"phenotypes":[],"markers":{}}' > model_config.json
    touch spec_report.html
    ${ProcessEnvelope.versionsStub(task.process, ['yaml'])}
    """
}
