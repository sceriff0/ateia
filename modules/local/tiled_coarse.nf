/*
 * TILED_COARSE - STARE fan-out step 1/4: global rigid anchor (M0) + tile plan (per moving slide).
 */
process TILED_COARSE {
    tag "${meta.patient_id}:${meta.channels.join('_')}"
    label 'process_low'

    container 'bolt3x/attend_image_analysis:tiled'

    input:
    tuple val(meta), path(reference, stageAs: 'ref/*'), path(moving, stageAs: 'mov/*')

    output:
    tuple val(meta), path("*_m0.json")   , emit: m0
    tuple val(meta), path("*_tiles.csv") , emit: tiles
    path "versions.yml"                  , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    def prefix     = "${meta.patient_id}_${meta.channels.join('_')}"
    def dapi_index = params.reg_tiled_dapi_index
    def tile       = params.reg_tiled_tile
    def halo       = params.reg_tiled_halo
    def max_dim    = params.reg_tiled_coarse_max_dim
    """
    tiled_coarse.py \\
        --reference ${reference} \\
        --moving ${moving} \\
        --dapi-index ${dapi_index} \\
        --tile ${tile} \\
        --halo ${halo} \\
        --max-dim ${max_dim} \\
        --out-m0 ${prefix}_m0.json \\
        --out-tiles ${prefix}_tiles.csv

    ${ProcessEnvelope.versions(task.process, ['skimage'])}
    """

    stub:
    def prefix = "${meta.patient_id}_${meta.channels.join('_')}"
    """
    echo '{"M0":[[1,0,0],[0,1,0],[0,0,1]],"ref_h":16,"ref_w":16,"ref_name":"ref","coarse_tre":0,"n_inliers":0}' > ${prefix}_m0.json
    printf 'ix,iy,cx,cy,x0,y0,x1,y1,rx0,ry0,rx1,ry1\\n0,0,8,8,0,0,16,16,0,0,16,16\\n' > ${prefix}_tiles.csv
    ${ProcessEnvelope.versionsStub(task.process, ['skimage'])}
    """
}
