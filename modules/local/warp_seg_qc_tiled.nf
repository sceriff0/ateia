/*
 * WARP_SEG_QC_TILED - staged segmentation-overlap QC (reg_qc = 2) for the tiled (STARE) method.
 *
 * The reg_qc=2 scorer is method-agnostic (bin/warp_seg_qc.py): it warps the reference and moving
 * native-cell GeoJSONs through each stage via an injected transform and scores per-pair IoU +
 * centroid residual against a correspondence fixed at the rigid anchor. This process is the tiled
 * counterpart of WARP_SEG_QC — it feeds `--method tiled`, so the warper is built from the STARE
 * transform manifest (M0 + mesh) instead of a VALIS registrar pickle.
 *
 * Stages are native/rigid/refined and always separable (the tiled method composes nothing
 * destructively), so no pre-micro stage checkpoint is needed. JVM-free: runs in the slim tiled
 * container, no BioFormats.
 */
process WARP_SEG_QC_TILED {
    tag "${meta.patient_id}:${moving_geojson.simpleName}"
    label 'process_low'

    container 'bolt3x/attend_image_analysis:tiled'

    input:
    tuple val(meta), path(manifest), val(ref_slide), val(moving_slide), path(ref_geojson), path(moving_geojson)

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
    """
    echo "${task.process},${meta.patient_id},${moving_geojson.name},0" > ${prefix}.WARP_SEG_QC_TILED.size.csv

    warp_seg_qc.py \\
        --method tiled \\
        --pickle ${manifest} \\
        --ref-slide '${ref_slide}' \\
        --moving-slide '${moving_slide}' \\
        --ref-geojson ${ref_geojson} \\
        --moving-geojson ${moving_geojson} \\
        --output ${prefix}_seg_qc.json \\
        --per-cell-csv ${prefix}_reg_residuals.csv \\
        --patient-id ${meta.patient_id} \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        scikit-image: \$(python -c "import skimage; print(skimage.__version__)" 2>/dev/null || echo "unknown")
        scipy: \$(python -c "import scipy; print(scipy.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    def prefix = "${meta.patient_id}_${moving_geojson.simpleName}"
    def stages = ['native', 'rigid', 'refined']
    def stage_stub = [n_pairs: 0, n_pairs_scored: 0, iou_n: 0, displacement_px_n: 0]
    def stub_json = groovy.json.JsonOutput.toJson([
        patient_id      : meta.patient_id,
        moving          : moving_slide,
        reference       : ref_slide,
        stages_separable: true,
        stage_order     : stages,
        stages          : stages.collectEntries { [(it): stage_stub] },
        delta_vs_anchor : (stages - 'rigid').collectEntries { [(it): [:]] },
        matching        : [method: 'mutual_nn_centroid', anchor_stage: 'rigid', n_pairs: 0],
        counts          : [features_ref: 0, features_moving: 0],
    ])
    """
    echo '${stub_json}' > ${prefix}_seg_qc.json
    printf 'moving,ref_x,ref_y,residual_px,stage\\n' > ${prefix}_reg_residuals.csv
    echo "STUB,${meta.patient_id},stub,0" > ${prefix}.WARP_SEG_QC_TILED.size.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        scikit-image: stub
        scipy: stub
    END_VERSIONS
    """
}
