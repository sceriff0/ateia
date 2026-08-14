/*
 * REDSEA_MATRIX - per-patient REDSEA compensation geometry
 *
 * REDSEA (Bai et al., Front. Immunol. 2021;12:652631) corrects lateral marker
 * spillover between touching cells. It splits into a part that depends only on
 * the segmentation mask and a part that depends on the channel, and this process
 * is the first half: which cells touch, how much perimeter each pair shares, and
 * which pixels lie in each cell's boundary band.
 *
 * WHY IT IS ITS OWN PROCESS. The mask half is identical for every one of a
 * patient's markers. Folding it into QUANTIFY would recompute the same mask pass
 * N_markers times; hoisting it here runs it once and lets the per-marker
 * compensation stay a sparse mat-vec inside the existing per-channel fan-out.
 * That is what makes REDSEA cheap here rather than a new serial stage.
 *
 * Input:  whole-cell segmentation mask, per patient
 * Output: <patient>_redsea.npz (geometry) + <patient>_redsea_qc.json (band-fraction
 *         diagnostics -- READ THESE, they are how --redsea_element_size is validated)
 */
process REDSEA_MATRIX {
    tag "${meta.patient_id}"

    container "bolt3x/attend_image_analysis:quantification_gpu"

    // meta, not a bare patient_id, for one concrete reason: this process's
    // publishDir closure in conf/modules.config has to be written in the one
    // literal shape tests/test_layout.py's parser can read, and that shape
    // interpolates meta.patient_id (see
    // test_every_publishdir_path_closure_matches_the_literal_shape_the_parser_reads).
    // A bare `val(patient_id)` would force an unparseable closure, and Layout's
    // kind-agreement check would then be blind to where this process publishes.
    input:
    tuple val(meta), path(cell_mask)

    output:
    tuple val(meta), path("${meta.patient_id}_redsea.npz"), emit: geometry
    path "${meta.patient_id}_redsea_qc.json"              , emit: qc
    path "versions.yml"                                   , emit: versions
    path "*.size.csv"                                     , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    """
    # Log input sizes for tracing (-L follows symlinks)
    mask_bytes=\$(stat -L --printf="%s" ${cell_mask} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${cell_mask.name},\${mask_bytes}" > ${meta.patient_id}.REDSEA_MATRIX.size.csv

    echo "Patient: ${meta.patient_id}"

    redsea_matrix.py \\
        --mask_file ${cell_mask} \\
        --prefix ${meta.patient_id} \\
        --outdir . \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['numpy', 'scipy'])}
    """

    stub:
    """
    touch ${meta.patient_id}_redsea.npz
    echo '{"n_cells": 0, "band_fraction_mean": 0.0, "element_size": 0}' > ${meta.patient_id}_redsea_qc.json
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}.REDSEA_MATRIX.size.csv

    ${ProcessEnvelope.versionsStub(task.process, ['numpy', 'scipy'])}
    """
}
