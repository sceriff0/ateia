process GENERATE_POSTPROCESSING_QC {
    tag "${meta.patient_id}"
    label 'process_medium'

    container 'bolt3x/attend_image_analysis:quantification_gpu'

    input:
    tuple val(meta), path(cell_mask), path(merged_csv)

    output:
    tuple val(meta), path("qc/*.png", optional: true), emit: qc
    path "versions.yml"              , emit: versions
    path("*.size.csv")               , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def prefix = task.ext.prefix ?: "${meta.patient_id}"
    """
    # Log input sizes for tracing (-L follows symlinks)
    mask_bytes=\$(stat -L --printf="%s" ${cell_mask} 2>/dev/null || echo 0)
    csv_bytes=\$(stat -L --printf="%s" ${merged_csv} 2>/dev/null || echo 0)
    total_bytes=\$((mask_bytes + csv_bytes))
    echo "${task.process},${meta.patient_id},${cell_mask.name}+${merged_csv.name},\${total_bytes}" > ${meta.patient_id}.GENERATE_POSTPROCESSING_QC.size.csv

    mkdir -p qc

    generate_postprocessing_qc.py \\
        --mask ${cell_mask} \\
        --csv ${merged_csv} \\
        --output qc \\
        --prefix ${prefix} \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        numpy: \$(python -c "import numpy; print(numpy.__version__)" 2>/dev/null || echo "unknown")
        pandas: \$(python -c "import pandas; print(pandas.__version__)" 2>/dev/null || echo "unknown")
        matplotlib: \$(python -c "import matplotlib; print(matplotlib.__version__)" 2>/dev/null || echo "unknown")
        scikit-image: \$(python -c "import skimage; print(skimage.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    def prefix = task.ext.prefix ?: "${meta.patient_id}"
    """
    mkdir -p qc
    touch qc/${prefix}_seg_overlay.png
    touch qc/${prefix}_cell_stats.png
    touch qc/${prefix}_intensity_distributions.png
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}.GENERATE_POSTPROCESSING_QC.size.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        numpy: stub
        pandas: stub
        matplotlib: stub
        scikit-image: stub
    END_VERSIONS
    """
}
