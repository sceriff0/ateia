/*
 * GENERATE_REGISTRATION_QC - render a before/after registration QC figure.
 *
 * Two composites on the REFERENCE canvas, origin-aligned: left is the reference
 * overlaid with the NATIVE (pre-registration) moving slide, right is the reference
 * overlaid with the REGISTERED one. The pair is what shows a reader what
 * registration corrected; the single "after" composite this used to render showed
 * only what it produced. Differing dimensions are reconciled by pad-or-crop, never
 * rescale -- see bin/utils/qc.py:compose_on_reference_canvas for why.
 *
 * The emit names are deliberately unchanged (qc/*_QC_RGB.{png,tif} and the optional
 * *_QC_RGB_fullres.tif): GENERATE_QC_REPORT and lib/Layout.groovy consume them, and
 * neither has to know the figure gained a panel.
 *
 * The overlay is built from the nuclear/fiducial channel, resolved per image from its
 * OME channel names against params.nuclear_markers (via MarkerUtils, the same rule
 * SPLIT_CHANNELS and SEGMENT use). Before this was plumbed through, bin/utils/qc.py
 * tested for the literal "DAPI" and fell back to channel 0 -- correct only because
 * CONVERT_IMAGE reserves channel 0 for the nuclear channel, and noisy on every
 * CELLTOX panel.
 *
 * The `native/` stageAs directory name is a pairing contract, not just a collision
 * guard: tests/subworkflows/local/registration.nf.test and
 * tests/subworkflows/add_cycle.nf.test both key their pairing assertions off it, so
 * renaming the convention means updating both.
 */
process GENERATE_REGISTRATION_QC {
    tag "${meta.patient_id}"
    label 'process_high'

    container "bolt3x/mirage-regqc:1.0.0"

    input:
    // `native` is a Groovy reserved word (a Java modifier keyword Groovy inherits), so the
    // native pre-registration image is bound as `native_image`. stageAs puts it in its own
    // subdirectory: three slides' artifacts now share one task directory and nothing
    // guarantees their basenames differ, so an unqualified stage is a latent collision.
    tuple val(meta), path(registered), path(native_image, stageAs: 'native/*'), path(reference)

    output:
    tuple val(meta), path("qc/*_QC_RGB.{png,tif}"), emit: qc
    tuple val(meta), path("qc/*_QC_RGB_fullres.tif"), optional: true, emit: qc_fullres
    path "versions.yml"                           , emit: versions
    path("*.size.csv")                            , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def scale_factor = params.qc_scale_factor
    // MarkerUtils.markerList also normalises the bare-String form Nextflow produces for
    // `--nuclear_markers CELLTOX`, so CLI and config spellings agree (see SPLIT_CHANNELS).
    def nuclear_args = "--nuclear-markers ${MarkerUtils.markerList(params.nuclear_markers).join(' ')}"
    """
    # Log input sizes for tracing (sum of registered + native + reference, -L follows symlinks)
    ${ProcessEnvelope.sizeLog(task.process, meta.patient_id, ["${registered}", "${native_image}", "${reference}"], "${meta.patient_id}_${registered.simpleName}.GENERATE_REGISTRATION_QC.size.csv")}

    mkdir -p qc

    generate_registration_qc.py \\
        --reference ${reference} \\
        --registered ${registered} \\
        --native ${native_image} \\
        --pixel-size-um ${meta.pixel_size} \\
        --output qc \\
        --scale-factor ${scale_factor} \\
        ${nuclear_args} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['numpy', 'tifffile'], task.container)}
    """

    stub:
    """
    mkdir -p qc
    touch qc/${registered.simpleName}_QC_RGB.png
    touch qc/${registered.simpleName}_QC_RGB_fullres.tif
    ${ProcessEnvelope.sizeLogStub(task.process, meta.patient_id, "${meta.patient_id}_${registered.simpleName}.GENERATE_REGISTRATION_QC.size.csv")}

    ${ProcessEnvelope.versionsStub(task.process, ['numpy', 'tifffile'], task.container)}
    """
}
