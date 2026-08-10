/*
 * SEGMENT - cell segmentation
 *
 * Dispatches between three backends, controlled by ``params.seg_method``:
 *   - 'stardist':   StarDist on the nuclear channel (channel-0 hard check).
 *   - 'instantseg': InstanSeg with the ``fluorescence_nuclei_and_cells`` model.
 *                   Channel-invariant: consumes the multichannel image directly.
 *   - 'cellsam':    CellSAM (SAM foundation model) on the resolved nuclear channel.
 *                   Segments nuclei; the whole-cell mask is derived by expanding
 *                   nuclei labels (StarDist-style).
 *
 * The backend is chosen ONCE, here, and everything that differs between the three
 * comes from ``lib/SegBackends.groovy``: container, entry point, resolved flags,
 * precondition guard, and versions rows. The tunables come from ``ext.args``
 * (``conf/modules.config``). This process body is backend-agnostic -- one size-log
 * preamble, one invocation, one versions heredoc -- so the shared parts cannot drift
 * apart between backends the way three copy-pasted script blocks did.
 *
 * All backends produce the same outputs (``*_nuclei_mask.tif`` / ``*_cell_mask.tif`` /
 * ``versions.yml`` / ``*.size.csv``) so all downstream modules are contract-preserving.
 */
process SEGMENT {
    tag "${meta.patient_id}"

    container { SegBackends.container(params.seg_method) }

    input:
    // seg_params is SegBackends.ctxParams(params) -- the SLICE of params the backends
    // read, resolved in subworkflows/local/segmentation.nf and arriving here as an
    // opaque value. It is an input rather than a `params` read in the script block
    // because Nextflow hashes the free variables a script block references: a bare
    // `params` there bound this task's cache key to EVERY pipeline parameter, so an
    // unrelated change (measured: --pyramid_resolutions) re-ran segmentation and
    // everything after it. Keeping the key list in SegBackends also keeps this process
    // body backend-agnostic, which tests/test_seg_backends.py enforces.
    tuple val(meta), path(merged_file), val(seg_params)

    output:
    tuple val(meta), path("*_nuclei_mask.tif"), emit: nuclei_mask
    tuple val(meta), path("*_cell_mask.tif")  , emit: cell_mask
    path "versions.yml"                        , emit: versions
    path("*.size.csv")                         , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def prefix = task.ext.prefix ?: "${meta.patient_id}"
    def backend = SegBackends.of(params.seg_method)
    def ctx = [meta: meta, prefix: prefix, params: seg_params]
    // Shell fragments arrive as lines and are indented HERE, by the block that owns the
    // indentation: Nextflow stripIndent()s the finished script, so a fragment carrying
    // its own leading whitespace would flatten the surrounding block.
    def guard = backend.guard(ctx).join('\n    ')
    def version_rows = backend.versions.join('\n        ')
    """
    # Log input size for tracing (-L follows symlinks)
    input_bytes=\$(stat -L --printf="%s" ${merged_file} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${merged_file.name},\${input_bytes}" > ${prefix}.SEGMENT.size.csv

    echo "Sample: ${meta.patient_id}"
    echo "Backend: ${params.seg_method} (attempt ${task.attempt})"
    echo "Args: ${args}"

    ${guard}

    ${backend.entrypoint} \\
        --image ${merged_file} \\
        --output-dir . \\
        ${backend.flags(ctx)} \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        ${version_rows}
    END_VERSIONS
    """

    stub:
    def prefix = task.ext.prefix ?: "${meta.patient_id}"
    """
    touch ${prefix}_nuclei_mask.tif
    touch ${prefix}_cell_mask.tif
    echo "STUB,${meta.patient_id},stub,0" > ${prefix}.SEGMENT.size.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        seg_method: ${params.seg_method}
    END_VERSIONS
    """
}
