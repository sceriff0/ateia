/*
 * ASHLAR_SOLVE - ASHLAR arm step 2/3: cross-cycle alignment -> a STARE transform manifest.
 *
 * Drives ashlar's EdgeAligner/LayerAligner directly rather than its CLI, because the CLI can
 * only write a mosaic and this arm needs the TRANSFORM: LayerAligner.positions rewritten as
 * M0 + mesh, the same manifest TILED_SOLVE emits. Downstream, ASHLAR_STITCH (TILED_STITCH
 * aliased) warps through it and WARP_SEG_QC scores it -- so ASHLAR is ranked on the reg_qc=2
 * segmentation-overlap metric, not a parallel table.
 *
 * This is the only step that needs the upstream ashlar image, which is amd64-only.
 */
process ASHLAR_SOLVE {
    tag "${meta.patient_id}:${meta.channels.join('_')}"
    label 'process_low'

    container 'labsyspharm/ashlar:1.20.0'

    input:
    tuple val(meta), path(ref_tiles, stageAs: 'ref_tiles'), path(mov_tiles, stageAs: 'mov_tiles'), val(ref_slide)

    output:
    tuple val(meta), path("*_manifest.json"), emit: manifest
    tuple val(meta), path("*_tre.json")     , emit: tre
    path "versions.yml"                     , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    def prefix    = "${meta.patient_id}_${meta.channels.join('_')}"
    def slidename = meta.channels.join('_')
    // The fiducial channel ashlar aligns on, resolved from THIS slide's channel metadata
    // exactly as TILED_COARSE does -- reg_tiled_nuclear_index is the shared explicit
    // override, not a per-backend one, so the two backends can never disagree about which
    // channel carries the nuclei.
    def nuclear_index = params.reg_tiled_nuclear_index != null
        ? params.reg_tiled_nuclear_index
        : MarkerUtils.nuclearIndex(meta.channels ?: [], params.nuclear_markers)
    if (nuclear_index < 0)
        throw new IllegalArgumentException(
            "${task.process}: no nuclear/fiducial channel among ${meta.channels} for " +
            "patient ${meta.patient_id}. Configured nuclear_markers: " +
            "${MarkerUtils.markerList(params.nuclear_markers).join(', ')}. " +
            "Set params.reg_tiled_nuclear_index to override.")
    def args = task.ext.args ?: ''
    """
    ashlar_solve.py \\
        --ref-tiles ${ref_tiles} \\
        --moving-tiles ${mov_tiles} \\
        --reference-name '${ref_slide}' \\
        --moving-name '${slidename}' \\
        --nuclear-index ${nuclear_index} \\
        --maximum-shift ${params.reg_ashlar_max_shift_um} \\
        ${args} \\
        --out-manifest ${prefix}_manifest.json \\
        --out-tre ${prefix}_tre.json

    ${ProcessEnvelope.versions(task.process, ['ashlar'])}
    """

    stub:
    def prefix    = "${meta.patient_id}_${meta.channels.join('_')}"
    def slidename = meta.channels.join('_')
    """
    echo '{"ref_slide":"ref","slides":{"ref":{"M0":[[1,0,0],[0,1,0],[0,0,1]],"mesh":null},"${slidename}":{"M0":[[1,0,0],[0,1,0],[0,0,1]],"mesh":null,"out_shape":[16,16]}}}' > ${prefix}_manifest.json
    echo '{"method":"ashlar","n_tiles":0,"n_discarded":0,"discard_fraction":0,"rigid_translation_xy":[0,0],"mesh_residual_px":{"p50":null,"p90":null,"max":null}}' > ${prefix}_tre.json
    ${ProcessEnvelope.versionsStub(task.process, ['ashlar'])}
    """
}
