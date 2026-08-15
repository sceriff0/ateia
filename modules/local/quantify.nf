/*
 * QUANTIFY - Marker intensity quantification, ONE TASK PER PATIENT
 *
 * Measures per-cell marker intensities from single-channel TIFFs using
 * segmentation masks, for ALL of a patient's markers in one invocation.
 *
 * WHY ONE TASK PER PATIENT, AND WHY THAT IS NOT A MEMORY REGRESSION
 * ----------------------------------------------------------------
 * This used to fan out per (patient x marker): 12 patients x 17 markers = 204
 * tasks, each asking for a flat 128 GB with executor.queueSize = 20, i.e. a peak
 * aggregate request near 2.5 TB — and each one re-staging and re-reading the same
 * whole-cell mask. It is now one task per patient.
 *
 * That is only safe because bin/quantify.py's batch loop holds the masks once and
 * ONE channel plane at a time: load -> compute -> discard, before the next channel
 * is read. Peak resident memory is therefore `masks + one plane`, exactly what a
 * single-marker task held, INDEPENDENT of the marker count. The naive shape —
 * stacking the patient's planes and then looping — produces byte-identical numbers
 * while multiplying peak memory by the marker count, so it would pass every
 * equivalence test in this repo and OOM on a real panel.
 * tests/test_quantify_batch.py is the guard: it instruments that one load seam and
 * asserts no earlier plane is still alive when the next is read.
 *
 * Input:  all of one patient's single-channel TIFFs + both masks (+ REDSEA geometry)
 * Output: one quantification CSV PER MARKER — the same files, with the same names,
 *         the per-marker fan-out published. Only the task count changed.
 */
process QUANTIFY {
    tag "${meta.patient_id}"

    container "bolt3x/attend_image_analysis:quantification_gpu"

    input:
    // `markers` is index-aligned with `channel_tiffs`: one entry per tiff, carrying
    // the marker's DECLARED name and the CSV filename it must write. Name and
    // filename travel as ONE map rather than two parallel lists so the pair cannot
    // come apart; bin/quantify.py refuses a length mismatch against the tiffs.
    tuple val(meta), val(markers), path(channel_tiffs), path(cell_mask), path(nuclei_mask), path(redsea_npz)

    output:
    tuple val(meta), path("*_quant.csv"), emit: individual_csv
    path "versions.yml"                 , emit: versions
    path("*.size.csv")                  , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def marker_list = markers instanceof List ? markers : [markers]
    def tiff_list = channel_tiffs instanceof List ? channel_tiffs : [channel_tiffs]
    // The markers' DECLARED names, resolved from meta.channels by
    // quantify_markers.nf's ChannelName.declaredFor -- NOT the tiffs' filename
    // stems. They fill the <marker> slot of the "<marker>: <Compartment>:
    // <Statistic>" key qupath-extension-flowpath parses case-sensitively, so they
    // must arrive at bin/quantify.py byte-for-byte as the samplesheet spelled them.
    //
    // Which is why they are POSIX-quoted rather than wrapped in hand-written single
    // quotes: a declared name is arbitrary samplesheet text, and until this seam
    // existed the value reaching here had already been through a filename
    // allowlist, so nothing unquotable could occur.
    def names_arg = ChannelName.shellList(marker_list*.name)
    // The output CSV names are built HERE, from meta.channel_stem, and passed in --
    // bin/quantify.py never derives a filename. Same rule as SPLIT_CHANNELS'
    // `--file-stems` (see lib/ChannelName.groovy, "ONE SANITISER, TWO LANGUAGES"):
    // a name two languages must agree on is computed once and handed over.
    def outputs_arg = marker_list*.output.join(' ')
    def tiffs_arg = tiff_list.join(' ')
    // Per-compartment quantification: route the nuclear mask in when enabled.
    // The mask path is only available here (not in modules.config ext.args), so the
    // toggle is read from params; the statistic list arrives via ext.args.
    def nuclei_arg = params.quantify_compartments ? "--nuclei_mask_file ${nuclei_mask}" : ''
    // REDSEA geometry is a required input so this process has ONE input arity
    // regardless of --redsea; when the feature is off the staged file is
    // assets/NO_REDSEA. Gating on the filename rather than on params keeps this
    // `script:` block from growing a second `params` reference (a bare `params`
    // here hashes the whole map -- see CLAUDE.md, "Verification reality"); the
    // marker list and REDSEAChecker arrive through ext.args instead.
    def redsea_arg = redsea_npz.name == 'NO_REDSEA' ? '' : "--redsea-geometry ${redsea_npz}"
    """
    # Log input sizes for tracing (all channel tiffs + cell mask, -L follows symlinks)
    tiff_bytes=\$(stat -L --printf="%s\\n" ${tiffs_arg} 2>/dev/null | awk '{sum+=\$1} END {print sum+0}')
    mask_bytes=\$(stat -L --printf="%s" ${cell_mask} 2>/dev/null || echo 0)
    total_bytes=\$((tiff_bytes + mask_bytes))
    echo "${task.process},${meta.patient_id},channels/+${cell_mask.name},\${total_bytes}" > ${meta.patient_id}.QUANTIFY.size.csv

    echo "Sample: ${meta.patient_id}"
    echo "Markers: "${names_arg}

    # Quantify every one of this patient's channels in a single process. The masks
    # are read once; the channel planes are read one at a time.
    quantify.py \\
        --channel_tiff ${tiffs_arg} \\
        --channel-name ${names_arg} \\
        --output_file ${outputs_arg} \\
        --mask_file ${cell_mask} \\
        ${nuclei_arg} \\
        --outdir . \\
        ${redsea_arg} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['pandas', 'skimage'])}
    """

    stub:
    def marker_list = markers instanceof List ? markers : [markers]
    def outputs_arg = marker_list*.output.join(' ')
    """
    for csv in ${outputs_arg}; do
        touch "\$csv"
    done
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}.QUANTIFY.size.csv

    ${ProcessEnvelope.versionsStub(task.process, ['pandas', 'skimage'])}
    """
}
