/*
 * WARP_SEG_QC - staged, fixed-correspondence segmentation-overlap QC (reg_qc = 2)
 *
 * Loads the VALIS registrar pickle and warps the reference and moving native-cell GeoJSONs
 * through each registration stage in turn — native, rigid, non-rigid, micro. Cell-to-cell
 * correspondence is established ONCE at the rigid stage (mutual-nearest centroid within a
 * nuclear radius) and then held fixed, so each stage's per-pair IoU and centroid residual
 * describe the same cells and the deltas are pure registration effects.
 *
 * The pre-micro stage checkpoint from REGISTER is optional but load-bearing: VALIS composes
 * the micro residual into the same displacement field, so without the checkpoint the
 * 'non_rigid' stage cannot be separated from 'micro' and is not reported (the output records
 * `stages_separable: false` and says why).
 *
 * Scoring is per-pair on a local window, so peak memory is one nucleus rather than one slide —
 * this process no longer allocates whole-slide label images.
 *
 * Runs in the VALIS container (same image as REGISTER's classic path, so the pickle loads and
 * scikit-image/scipy are present).
 *
 * Classic path only: the distributed registration path produces no registrar pickle.
 */
process WARP_SEG_QC {
    tag "${meta.patient_id}:${moving_geojson.simpleName}"
    label 'process_medium'

    container "cdgatenbee/valis-wsi:1.0.0"

    input:
    tuple val(meta), path(pickle), val(ref_slide), val(moving_slide), path(ref_geojson), path(moving_geojson), path(stage_checkpoint)

    output:
    tuple val(meta), path("*_seg_qc.json"), emit: metrics
    // Final-stage per-cell registration residuals, spatially joinable onto cell_mask.
    tuple val(meta), path("*_reg_residuals.csv"), optional: true, emit: per_cell
    path "versions.yml"                   , emit: versions
    path("*.size.csv")                    , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def prefix = "${meta.patient_id}_${moving_geojson.simpleName}"
    // stage_checkpoint is staged as a directory; absent when REGISTER did not emit one
    // (reg_qc < 2 on the registering run, or the snapshot failed and was logged as a warning).
    def ckpt_arg = stage_checkpoint ? "--checkpoint-dir ${stage_checkpoint}" : ''
    // Tell the QC the run's micro depth so it can state honestly whether 'rigid' includes
    // micro-rigid (level >= 1). Single-source via ParamUtils, same as REGISTER.
    def micro_reg = ParamUtils.microRegLevel(params)
    """
    echo "${task.process},${meta.patient_id},${moving_geojson.name},0" > ${prefix}.WARP_SEG_QC.size.csv

    warp_seg_qc.py \\
        --pickle ${pickle} \\
        --ref-slide '${ref_slide}' \\
        --moving-slide '${moving_slide}' \\
        --ref-geojson ${ref_geojson} \\
        --moving-geojson ${moving_geojson} \\
        --output ${prefix}_seg_qc.json \\
        --per-cell-csv ${prefix}_reg_residuals.csv \\
        --patient-id ${meta.patient_id} \\
        --moving-name '${moving_slide}' \\
        --reference-name '${ref_slide}' \\
        --micro-reg ${micro_reg} \\
        ${ckpt_arg} \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        valis: \$(python -c "import valis; print(valis.__version__)" 2>/dev/null || echo "unknown")
        scikit-image: \$(python -c "import skimage; print(skimage.__version__)" 2>/dev/null || echo "unknown")
        scipy: \$(python -c "import scipy; print(scipy.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    def prefix = "${meta.patient_id}_${moving_geojson.simpleName}"
    // Built in Groovy rather than inline in the shell string: the record is nested enough that
    // a hand-written JSON literal in a heredoc is a quoting accident waiting to happen.
    def stage_stub = [n_pairs: 0, n_pairs_scored: 0, iou_n: 0, displacement_px_n: 0]
    def stages = ['native', 'rigid', 'non_rigid', 'micro']
    def micro_reg = ParamUtils.microRegLevel(params)
    def stub_json = groovy.json.JsonOutput.toJson([
        patient_id      : meta.patient_id,
        moving          : moving_slide,
        reference       : ref_slide,
        stages_separable: true,
        micro_reg       : micro_reg,
        rigid_includes_micro_rigid: micro_reg >= 1,
        stage_order     : stages,
        stages          : stages.collectEntries { [(it): stage_stub] },
        delta_vs_anchor : (stages - 'rigid').collectEntries { [(it): [:]] },
        matching        : [method: 'mutual_nn_centroid', anchor_stage: 'rigid', n_pairs: 0],
        counts          : [features_ref: 0, features_moving: 0],
    ])
    """
    echo '${stub_json}' > ${prefix}_seg_qc.json
    printf 'moving,ref_x,ref_y,residual_px,stage\\n' > ${prefix}_reg_residuals.csv
    echo "STUB,${meta.patient_id},stub,0" > ${prefix}.WARP_SEG_QC.size.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        valis: stub
        scikit-image: stub
        scipy: stub
    END_VERSIONS
    """
}
