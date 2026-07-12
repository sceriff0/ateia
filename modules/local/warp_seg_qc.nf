/*
 * WARP_SEG_QC - warp native cell polygons through the registrar + score overlap (reg_qc = 2)
 *
 * Loads the VALIS registrar pickle and warps the reference and moving native-cell GeoJSONs
 * into the aligned frame, scoring nuclei-mask overlap before (raw overlay) vs after (warped)
 * registration -> {before, after, delta} Dice/IoU/instance-F1. Runs in the VALIS container
 * (same image as REGISTER's classic path, so the pickle loads and scikit-image is present).
 *
 * Classic path only: the distributed registration path produces no registrar pickle.
 */
process WARP_SEG_QC {
    tag "${meta.patient_id}:${moving_geojson.simpleName}"
    label 'process_medium'

    container "cdgatenbee/valis-wsi:1.0.0"

    input:
    tuple val(meta), path(pickle), val(ref_slide), val(moving_slide), path(ref_geojson), path(moving_geojson)

    output:
    tuple val(meta), path("*_seg_qc.json"), emit: metrics
    path "versions.yml"                   , emit: versions
    path("*.size.csv")                    , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def prefix = "${meta.patient_id}_${moving_geojson.simpleName}"
    """
    echo "${task.process},${meta.patient_id},${moving_geojson.name},0" > ${prefix}.WARP_SEG_QC.size.csv

    warp_seg_qc.py \\
        --pickle ${pickle} \\
        --ref-slide '${ref_slide}' \\
        --moving-slide '${moving_slide}' \\
        --ref-geojson ${ref_geojson} \\
        --moving-geojson ${moving_geojson} \\
        --output ${prefix}_seg_qc.json \\
        --patient-id ${meta.patient_id} \\
        --moving-name '${moving_slide}' \\
        --reference-name '${ref_slide}' \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        valis: \$(python -c "import valis; print(valis.__version__)" 2>/dev/null || echo "unknown")
        scikit-image: \$(python -c "import skimage; print(skimage.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    def prefix = "${meta.patient_id}_${moving_geojson.simpleName}"
    """
    echo '{"patient_id": "${meta.patient_id}", "moving": "${moving_slide}", "reference": "${ref_slide}", "before": {"dice": 0.0, "iou": 0.0, "instance_f1": 0.0}, "after": {"dice": 0.0, "iou": 0.0, "instance_f1": 0.0}, "delta": {"dice": 0.0, "iou": 0.0, "instance_f1": 0.0}}' > ${prefix}_seg_qc.json
    echo "STUB,${meta.patient_id},stub,0" > ${prefix}.WARP_SEG_QC.size.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        valis: stub
        scikit-image: stub
    END_VERSIONS
    """
}
