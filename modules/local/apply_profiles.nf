/*
 * APPLY_PROFILES - apply BASICPY's illumination profiles and reassemble the slide
 *
 * nf-core's BASICPY module computes PROFILES ONLY; upstream, mcmicro applies them
 * downstream inside ASHLAR. mirage has no ASHLAR, so this process is the missing half:
 *
 *     corrected = (image - darkfield) / flatfield
 *
 * per channel, per pseudo-FOV, reassembled from TILE_FOR_BASIC's positions sidecar.
 *
 * Three contracts carried over from the in-process BaSiC path, all in
 * bin/apply_basic_profiles.py: the nuclear/fiducial channel is left uncorrected (the
 * decision is TILE_FOR_BASIC's, recorded in the sidecar), negatives are clipped through
 * bin/utils/validation.py's clip_negative_values in ONE aggregate log line, and the
 * float->integer store rounds before casting.
 *
 * Input:  the slide, its tile-position sidecar, and BASICPY's *-dfp / *-ffp profiles
 * Output: <name>_corrected.ome.tif -- the same artifact PREPROCESS published, in the same
 *         place, so the preprocessed checkpoint row is unchanged.
 */
process APPLY_PROFILES {
    tag "${meta.patient_id}"

    container "bolt3x/mirage-preprocess:1.0.0"

    input:
    tuple val(meta), path(ome_tiff), path(sidecar), path(darkfield), path(flatfield)

    output:
    tuple val(meta), path("*_corrected.ome.tif"), emit: preprocessed
    path "versions.yml"                         , emit: versions
    path("*.size.csv")                          , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    """
    # Log input size for tracing (-L follows symlinks)
    input_bytes=\$(stat -L --printf="%s" ${ome_tiff} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${ome_tiff.name},\${input_bytes}" > ${meta.patient_id}_${ome_tiff.simpleName}.APPLY_PROFILES.size.csv

    apply_basic_profiles.py \\
        --image ${ome_tiff} \\
        --sidecar ${sidecar} \\
        --flatfield ${flatfield} \\
        --darkfield ${darkfield} \\
        --output ${ome_tiff.simpleName}_corrected.ome.tif \\
        --pixel-size ${params.pixel_size} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['numpy', 'tifffile'])}
    """

    stub:
    """
    touch ${ome_tiff.simpleName}_corrected.ome.tif
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}_${ome_tiff.simpleName}.APPLY_PROFILES.size.csv

    ${ProcessEnvelope.versionsStub(task.process, ['numpy', 'tifffile'])}
    """
}
