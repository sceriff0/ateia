/*
 * SEGMENTATION_QC - segmentation-based registration QC (reg_qc = 2)
 *
 * Segments the DAPI channel of the reference and a moving slide independently
 * (StarDist), before AND after registration, and scores nuclei-mask overlap
 * (Dice / IoU / instance-F1):
 *   - after  = overlap of the two REGISTERED slides (aligned grid, compared directly)
 *   - before = overlap of the two UNREGISTERED slides (baseline misalignment)
 *   - delta  = after - before (the registration improvement)
 * Complements the DAPI-overlay image QC (GENERATE_REGISTRATION_QC) with a metric.
 *
 * Reuses the same StarDist container + model params as SEGMENT.
 */
process SEGMENTATION_QC {
    tag "${meta.patient_id}"
    label 'process_high'

    container "bolt3x/attend_image_analysis:segmentation_gpu"

    input:
    tuple val(meta), path(reference_registered), path(moving_registered), path(reference_pre), path(moving_pre)

    output:
    tuple val(meta), path("*_seg_qc.json"), emit: metrics
    path "versions.yml"                   , emit: versions
    path("*.size.csv")                    , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def prefix = "${meta.patient_id}_${moving_registered.simpleName}"
    def use_gpu_flag = params.seg_gpu ? '--use-gpu' : ''
    def pmin = params.seg_pmin ?: 1.0
    def pmax = params.seg_pmax ?: 99.8
    def n_tiles_y = params.seg_n_tiles_y ?: 1
    def n_tiles_x = params.seg_n_tiles_x ?: 1
    // Pass --model-dir only when a custom StarDist model is configured; otherwise
    // segmentation_qc.py loads --model-name as a built-in pretrained model.
    def model_dir_arg = params.segmentation_model_dir ? "--model-dir ${params.segmentation_model_dir}" : ''
    def prob_arg = (params.seg_prob_thresh != null) ? "--prob-thresh ${params.seg_prob_thresh}" : ''
    """
    total_bytes=0
    for f in ${reference_registered} ${moving_registered} ${reference_pre} ${moving_pre}; do
        b=\$(stat -L --printf="%s" "\$f" 2>/dev/null || echo 0); total_bytes=\$((total_bytes + b))
    done
    echo "${task.process},${meta.patient_id},${moving_registered.name},\${total_bytes}" > ${prefix}.SEGMENTATION_QC.size.csv

    segmentation_qc.py \\
        --reference-registered ${reference_registered} \\
        --moving-registered ${moving_registered} \\
        --reference-pre ${reference_pre} \\
        --moving-pre ${moving_pre} \\
        --output ${prefix}_seg_qc.json \\
        --patient-id ${meta.patient_id} \\
        --model-name ${params.segmentation_model} \\
        ${model_dir_arg} \\
        ${use_gpu_flag} \\
        --pmin ${pmin} --pmax ${pmax} \\
        --n-tiles ${n_tiles_y} ${n_tiles_x} \\
        ${prob_arg} \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        numpy: \$(python -c "import numpy; print(numpy.__version__)" 2>/dev/null || echo "unknown")
        stardist: \$(python -c "import stardist; print(stardist.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    def prefix = "${meta.patient_id}_${moving_registered.simpleName}"
    """
    echo '{"patient_id": "${meta.patient_id}", "moving": "${moving_registered.name}", "reference": "${reference_registered.name}", "before": {"dice": 0.0, "iou": 0.0, "instance_f1": 0.0}, "after": {"dice": 0.0, "iou": 0.0, "instance_f1": 0.0}, "delta": {"dice": 0.0, "iou": 0.0, "instance_f1": 0.0}}' > ${prefix}_seg_qc.json
    echo "STUB,${meta.patient_id},stub,0" > ${prefix}.SEGMENTATION_QC.size.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        numpy: stub
        stardist: stub
    END_VERSIONS
    """
}
