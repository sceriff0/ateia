/*
 * TILE_FOR_BASIC - write a stitched slide as a multi-SITE OME-TIFF
 *
 * nf-core's BASICPY module refuses a single-sited image by design: its entrypoint builds
 * the field-of-view axis as `istack.stack(I=('M','T','Z'))` and raises
 * "The image is single sited. Was it saved in the correct way?" when `len(I) < 2`. A
 * mirage slide is one stitched plane per channel, so SizeM = SizeT = SizeZ = 1 and it is
 * refused.
 *
 * This process writes mirage's existing non-overlapping FOV grid (params.preproc_tile_size,
 * the same grid the in-process BaSiC path fitted on) onto the Z axis, so the module sees
 * one site per tile. Axes are CZYX, not ZCYX, because the module iterates CHANNELS and
 * fits one profile per channel.
 *
 * It also decides the nuclear/fiducial skip ONCE and records it in a JSON sidecar, so
 * APPLY_PROFILES reads the answer rather than re-deriving it.
 *
 * Input:  standardised OME-TIFF (C, Y, X) with channel metadata
 * Output: <name>_tiles.ome.tif (CZYX tile stack) + <name>_tiles.json (positions sidecar),
 *         and the input slide passed through so APPLY_PROFILES gets all three together
 *         without a meta-keyed join (two slides of one patient can share an identical
 *         meta map, which would mis-pair).
 */
process TILE_FOR_BASIC {
    tag "${meta.patient_id}"

    container "bolt3x/mirage-preprocess:1.0.0"

    input:
    tuple val(meta), path(ome_tiff)

    output:
    tuple val(meta), path("*_tiles.ome.tif")                    , emit: tiles
    tuple val(meta), path(ome_tiff), path("*_tiles.json")       , emit: sidecar
    path "versions.yml"                                         , emit: versions
    path("*.size.csv")                                          , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def skip_nuclear_flag = params.preproc_skip_nuclear ? '--skip_nuclear' : ''
    // Same spelling, same reason, as PREPROCESS and SPLIT_CHANNELS: MarkerUtils.markerList
    // is the one sanctioned reader of params.nuclear_markers -- it normalises the bare-String
    // form Nextflow produces for `--nuclear_markers CELLTOX` and the one-element comma-joined
    // form a params file can produce, both of which fail SILENTLY if read raw
    // (tests/test_nuclear_marker_routing.py).
    def nuclear_args = "--nuclear-markers ${MarkerUtils.markerList(params.nuclear_markers).join(' ')}"
    def channels = meta.channels.join(' ')
    """
    ${ProcessEnvelope.sizeLog(task.process, meta.patient_id, ["${ome_tiff}"], "${meta.patient_id}_${ome_tiff.simpleName}.TILE_FOR_BASIC.size.csv")}

    tile_for_basic.py \\
        --image ${ome_tiff} \\
        --output ${ome_tiff.simpleName}_tiles.ome.tif \\
        --sidecar ${ome_tiff.simpleName}_tiles.json \\
        --channels ${channels} \\
        --fov_size ${params.preproc_tile_size} \\
        ${skip_nuclear_flag} \\
        ${nuclear_args} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['numpy', 'tifffile'], task.container)}
    """

    stub:
    """
    touch ${ome_tiff.simpleName}_tiles.ome.tif
    echo '{"format_version": 1}' > ${ome_tiff.simpleName}_tiles.json
    ${ProcessEnvelope.sizeLogStub(task.process, meta.patient_id, "${meta.patient_id}_${ome_tiff.simpleName}.TILE_FOR_BASIC.size.csv")}

    ${ProcessEnvelope.versionsStub(task.process, ['numpy', 'tifffile'], task.container)}
    """
}
