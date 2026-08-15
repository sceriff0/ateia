/*
 * EXTRACT_PROPERTIES - Morphology + simplified polygon contours from a segmentation mask
 *
 * ONE process, TWO aliases. This used to be two module files -- extract_cell_properties.nf
 * and extract_nuclei_properties.nf -- differing in exactly six mechanical ways: the process
 * name, one extra input, the `--reference_mask` flag, the output prefix, the size-log
 * variable, and the stub's subdirectory. Same container, same entrypoint script, same emit
 * shape. That is a parameter, not a second file.
 *
 *   EXTRACT_CELL_PROPERTIES    the cell mask, no reference, outputs at the task root
 *                              -> <pid>/cell_properties/{morphology.csv,contours.json}
 *                              Runs ONCE per patient after SEGMENT so regionprops is
 *                              computed once here instead of N times across QUANTIFY,
 *                              and so EXPORT_GEOJSON has polygons.
 *
 *   EXTRACT_NUCLEI_PROPERTIES  the nuclear mask, with the whole-cell mask as
 *                              --reference_mask so the emitted contours.json is keyed by
 *                              CELL label (EXPORT_GEOJSON then attaches each nucleus
 *                              polygon to its cell by plain identity lookup). Gated on
 *                              --quantify_compartments. Outputs under `outsubdir`
 *                              -> <pid>/cell_properties/nuclei/{...}
 *                              Its morphology.csv is nucleus morphology and is unused
 *                              downstream.
 *
 * An alias is not decoration: DSL2 refuses to invoke the same process twice in one
 * workflow, and subworkflows/local/segmentation.nf invokes this twice.
 *
 * WHY `outsubdir` IS AN INPUT AND NOT A GUESS. publishDir carries a task-relative
 * subdirectory into the published path, so writing into `<outsubdir>/` is exactly what
 * puts these files at <pid>/cell_properties/<outsubdir>/. The old nuclei module created
 * its subdirectory for the opposite reason -- its own comment said the `mkdir -p` existed
 * ONLY so that a downstream consumer, Layout.publishedPath's `producerSubdir`, could
 * recover the segment by reading the parent directory's name back off the task directory.
 * The producer was contorted to make a consumer-side string heuristic work. Now the CALLER
 * names the subdirectory (Layout.NUCLEI_SUBDIR), passes it here, and passes the same value
 * to Layout.publishedPath's explicit-subdir overload when it records the checkpoint row.
 * Nothing about these outputs is inferred from directory shape.
 *
 * WHY `reference_mask` IS MANDATORY WITH A SENTINEL. assets/NO_REFERENCE_MASK is staged
 * when there is no reference, and the flag is gated on the filename -- the same idiom
 * modules/local/quantify.nf uses for assets/NO_REDSEA. One input arity for both aliases,
 * no optional-input branch in the channel graph, and no second `params` reference in this
 * script: block (a bare `params` here would hash the whole map; see CLAUDE.md).
 *
 * Input:  mask + reference mask (or the sentinel) + the output subdirectory ('' = task root)
 * Output: morphology.csv + contours.json + the per-alias size log, all under `outsubdir`
 */
process EXTRACT_PROPERTIES {
    tag "${meta.patient_id}"

    container "bolt3x/attend_image_analysis:quantification_gpu"

    input:
    tuple val(meta), path(mask), path(reference_mask)
    val outsubdir

    output:
    tuple val(meta), path("${outsubdir ? outsubdir + '/' : ''}morphology.csv"), emit: morphology
    tuple val(meta), path("${outsubdir ? outsubdir + '/' : ''}contours.json") , emit: contours
    path "versions.yml"                                                      , emit: versions
    path("${outsubdir ? outsubdir + '/' : ''}*.size.csv")                    , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args       = task.ext.args ?: ''
    def outdir_rel = outsubdir ?: '.'
    // task.process is the FULLY QUALIFIED name of THIS invocation, so it carries the
    // alias. The size log keeps the exact filename it had when these were two processes
    // (<pid>.EXTRACT_CELL_PROPERTIES.size.csv / <pid>.EXTRACT_NUCLEI_PROPERTIES.size.csv);
    // both are PUBLISHED artifacts, so one hardcoded name for two aliases would rename one.
    def alias    = task.process.tokenize(':').last()
    def size_log = outsubdir ? "${outsubdir}/${meta.patient_id}.${alias}.size.csv"
                             : "${meta.patient_id}.${alias}.size.csv"
    def ref_arg  = reference_mask.name == 'NO_REFERENCE_MASK' ? '' : "--reference_mask ${reference_mask}"
    """
    # No-op when outdir_rel is '.'; creates the caller's subdirectory otherwise. The size
    # log goes in there too, so the whole process publishes as ONE unit rather than
    # splitting its diagnostic artifact into a different published directory than its
    # real outputs.
    mkdir -p ${outdir_rel}

    # Log input size for tracing (-L follows symlinks)
    input_bytes=\$(stat -L --printf="%s" ${mask} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${mask.name},\${input_bytes}" > ${size_log}

    echo "Sample: ${meta.patient_id}"

    extract_cell_properties.py \\
        --mask_file ${mask} \\
        ${ref_arg} \\
        --outdir ${outdir_rel} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['skimage'])}
    """

    stub:
    def outdir_rel = outsubdir ?: '.'
    def alias      = task.process.tokenize(':').last()
    def size_log   = outsubdir ? "${outsubdir}/${meta.patient_id}.${alias}.size.csv"
                               : "${meta.patient_id}.${alias}.size.csv"
    """
    mkdir -p ${outdir_rel}
    touch ${outdir_rel}/morphology.csv ${outdir_rel}/contours.json
    echo "STUB,${meta.patient_id},stub,0" > ${size_log}

    ${ProcessEnvelope.versionsStub(task.process, ['skimage'])}
    """
}
