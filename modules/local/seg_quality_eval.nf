/*
 * SEG_QUALITY_EVAL - reference-free cell-segmentation quality scoring (CSE, 2D).
 * Scores each patient's cell+nucleus mask against its reference image and emits
 * a per-patient metrics JSON (informational QC; no gating).
 */
process SEG_QUALITY_EVAL {
    tag "${meta.patient_id}"
    label 'process_high'

    container "bolt3x/attend_image_analysis:${params.segeval_tag}"

    input:
    tuple val(meta), path(cell_mask), path(nuclei_mask), path(image)

    output:
    tuple val(meta), path("*_seg_eval.json"), emit: metrics
    path "versions.yml"                      , emit: versions
    path("*.size.csv")                       , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def prefix = "${meta.patient_id}"
    def px = params.cse_pixel_size_um ?: params.pixel_size
    def px_arg = px ? "--pixel-size-um ${px}" : ''
    """
    bytes=\$(stat -L --printf="%s" ${image} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${image.name},\${bytes}" > ${prefix}.SEG_QUALITY_EVAL.size.csv

    seg_quality_eval.py \\
        --cell-mask ${cell_mask} \\
        --nuclei-mask ${nuclei_mask} \\
        --image ${image} \\
        --id ${prefix} \\
        --out ${prefix}_seg_eval.json \\
        ${px_arg} \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python3 --version 2>&1 | sed 's/Python //')
        scikit-learn: \$(python3 -c "import sklearn; print(sklearn.__version__)" 2>/dev/null || echo "unknown")
        scipy: \$(python3 -c "import scipy; print(scipy.__version__)" 2>/dev/null || echo "unknown")
        CellSegmentationEvaluator: \$(python3 -c "import sys,os; sys.path.insert(0, os.path.join('.','utils')); import cse; print(cse.__cse_upstream_version__)" 2>/dev/null || echo "1.5.19-vendored")
    END_VERSIONS
    """

    stub:
    def prefix = "${meta.patient_id}"
    """
    echo '{"id": "${prefix}", "QualityScore": 0.0, "metrics": {}}' > ${prefix}_seg_eval.json
    echo "STUB,${meta.patient_id},stub,0" > ${prefix}.SEG_QUALITY_EVAL.size.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        CellSegmentationEvaluator: 1.5.19-vendored
    END_VERSIONS
    """
}
