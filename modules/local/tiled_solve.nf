/*
 * TILED_SOLVE - STARE fan-out step 3/4: assemble the manifest from per-tile control points.
 *
 * One cheap per-slide reduction (kilobytes): gathers every tile's control point and writes the
 * self-contained transform manifest (M0 + TRE-gated mesh) the stitch and reg_qc=2 warper consume.
 */
process TILED_SOLVE {
    tag "${meta.patient_id}:${meta.channels.join('_')}"
    label 'process_single'

    container 'bolt3x/attend_image_analysis:tiled'

    input:
    tuple val(meta), path(m0), path(controls, stageAs: 'ctrl_?/*')

    output:
    tuple val(meta), path("*_manifest.json"), emit: manifest
    path "versions.yml"                     , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    def prefix    = "${meta.patient_id}_${meta.channels.join('_')}"
    def slidename = meta.channels.join('_')
    def gate      = params.reg_tiled_gate_tre ?: 1.0
    """
    tiled_solve.py \\
        --m0 ${m0} \\
        --controls 'ctrl_*/*_ctrl.json' \\
        --gate-tre ${gate} \\
        --moving-name '${slidename}' \\
        --out-manifest ${prefix}_manifest.json

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
    END_VERSIONS
    """

    stub:
    def prefix    = "${meta.patient_id}_${meta.channels.join('_')}"
    def slidename = meta.channels.join('_')
    """
    echo '{"ref_slide":"ref","slides":{"ref":{"M0":[[1,0,0],[0,1,0],[0,0,1]],"mesh":null},"${slidename}":{"M0":[[1,0,0],[0,1,0],[0,0,1]],"mesh":null,"out_shape":[16,16]}}}' > ${prefix}_manifest.json
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
    END_VERSIONS
    """
}
