/*
 * SEG_QUALITY_EVAL - reference-free cell-segmentation quality scoring (CSE, 2D).
 * Scores each patient's cell+nucleus mask against its reference image and emits
 * a per-patient metrics JSON (informational QC; no gating).
 */
process SEG_QUALITY_EVAL {
    tag "${meta.patient_id}"

    container "bolt3x/mirage-segeval:${params.segeval_tag}"

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
    // meta.pixel_size, not params.pixel_size, for the fallback: bin/seg_quality_eval.py
    // does a bare float() on this value and never reads scale from `image` itself (see
    // that script's own note), so the literal string 'auto' (the shipped
    // params.pixel_size default) raises ValueError -- and this process's
    // retry-then-drop errorStrategy SWALLOWS that, silently dropping the artifact
    // instead of failing the run. meta.pixel_size is the number INPUT_CHECK's
    // PREFLIGHT_SCALE already resolved per-slide. cse_pixel_size_um still takes
    // precedence when the operator explicitly sets it.
    def px = params.cse_pixel_size_um != null ? params.cse_pixel_size_um : meta.pixel_size
    // THROW, do not silently omit. `px ? "--pixel-size-um ${px}" : ''` used to render
    // an absent flag when px was null -- Groovy null is FALSY, so the ternary's ''
    // branch fired instead of the script ever seeing `null` -- and this process's
    // retry-then-drop errorStrategy (conf/modules.config) retried three times against
    // an input that could never change, then silently dropped the artifact: a green
    // run missing a QC file with no error anywhere. Every checkpoint now records a
    // real pixel_size (lib/Checkpoint.groovy), so a null one here means a genuine bug
    // upstream, not a value this process should quietly work around.
    if (px == null)
        throw new IllegalStateException(
            "SEG_QUALITY_EVAL(${meta.patient_id}): no pixel size available -- meta.pixel_size is " +
            "null and --cse_pixel_size_um was not set. Every meta reaching this process should " +
            "carry a resolved scale by now; a null value here is an upstream bug to fix, not a " +
            "flag to omit.")
    def px_arg = "--pixel-size-um ${px}"
    """
    ${ProcessEnvelope.sizeLog(task.process, meta.patient_id, ["${image}"], "${prefix}.SEG_QUALITY_EVAL.size.csv")}

    seg_quality_eval.py \\
        --cell-mask ${cell_mask} \\
        --nuclei-mask ${nuclei_mask} \\
        --image ${image} \\
        --id ${prefix} \\
        --out ${prefix}_seg_eval.json \\
        ${px_arg} \\
        ${args}
    ${ProcessEnvelope.versions(task.process, ['python', 'scikit-learn', 'scipy', 'CellSegmentationEvaluator'], task.container)}
    """

    stub:
    def prefix = "${meta.patient_id}"
    """
    echo '{"id": "${prefix}", "QualityScore": 0.0, "metrics": {}}' > ${prefix}_seg_eval.json
    ${ProcessEnvelope.sizeLogStub(task.process, meta.patient_id, "${prefix}.SEG_QUALITY_EVAL.size.csv")}
    ${ProcessEnvelope.versionsStub(task.process, ['python', 'scikit-learn', 'scipy', 'CellSegmentationEvaluator'], task.container)}
    """
}
