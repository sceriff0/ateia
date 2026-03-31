process GENERATE_QC_REPORT {
    tag "qc_report"
    label 'process_single'

    container 'bolt3x/attend_image_analysis:preprocess'

    input:
    path(preprocess_qc_pngs, stageAs: 'preprocess_qc/*')
    path(registration_qc_pngs, stageAs: 'registration_qc/*')
    path(feature_distance_jsons, stageAs: 'feature_dist/*')
    path(valis_summary_csvs, stageAs: 'valis_summary/*')
    path(postprocess_qc_pngs, stageAs: 'postprocess_qc/*')
    path(versions_yml)

    output:
    path "mirage_qc_report_*.html", emit: report
    path "mirage_qc_data_*/"      , emit: data, optional: true
    path "versions.yml"            , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def timestamp = new java.text.SimpleDateFormat("yyyyMMdd_HHmmss").format(new Date())
    """
    generate_qc_report.py \\
        --preprocess-qc preprocess_qc/ \\
        --registration-qc registration_qc/ \\
        --feature-distances feature_dist/ \\
        --valis-summary valis_summary/ \\
        --postprocess-qc postprocess_qc/ \\
        --versions ${versions_yml} \\
        --output mirage_qc_report_${timestamp}.html \\
        --data-dir mirage_qc_data_${timestamp}/ \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
    END_VERSIONS
    """

    stub:
    def timestamp = new java.text.SimpleDateFormat("yyyyMMdd_HHmmss").format(new Date())
    """
    mkdir -p mirage_qc_data_${timestamp}
    touch mirage_qc_report_${timestamp}.html

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
    END_VERSIONS
    """
}
