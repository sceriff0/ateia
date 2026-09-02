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
 * precondition guard, and the tool names for versions.yml. The tunables come from
 * ``ext.args`` (``conf/modules.config``). This process body is backend-agnostic -- one
 * size-log preamble, one invocation, one versions envelope -- so the shared parts cannot
 * drift apart between backends the way three copy-pasted script blocks did.
 *
 * versions.yml IS RENDERED BY ``lib/ProcessEnvelope.groovy``, IN BOTH BLOCKS, FROM ONE
 * LIST. Until this was done, ``script:`` and ``stub:`` each hand-wrote their own heredoc
 * and they named DISJOINT keys: script: reported ``python`` plus the backend's real tools
 * (deepcell/tensorflow, instanseg/torch, cellSAM/torch), stub: reported ``python`` plus a
 * bare ``seg_method: <name>`` that is not a tool at all. ``-stub`` never evaluates a
 * ``script:`` block, and CI's blocking gate is ``nf-test --tag stub``, so the branch that
 * ships was the branch nothing ran. Both blocks now pass ``backend.versionTools``, and
 * both resolve ``backend`` from the same ``SegBackends.of(params.seg_method)`` expression.
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
    """
    ${ProcessEnvelope.sizeLog(task.process, meta.patient_id, ["${merged_file}"], "${prefix}.SEGMENT.size.csv")}

    echo "Sample: ${meta.patient_id}"
    echo "Backend: ${params.seg_method} (attempt ${task.attempt})"
    echo "Args: ${args}"

    ${guard}

    ${backend.entrypoint} \\
        --image ${merged_file} \\
        --output-dir . \\
        ${backend.flags(ctx)} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, backend.versionTools, task.container)}
    """

    stub:
    def prefix = task.ext.prefix ?: "${meta.patient_id}"
    // Resolved from the SAME expression as script:'s binding above, which is the whole
    // point -- tests/test_versions_envelope.py compares the two `SegBackends.of(...)`
    // call sites textually, because two identical `backend.versionTools` reads prove
    // nothing if `backend` was bound to different backends in the two blocks.
    def backend = SegBackends.of(params.seg_method)
    """
    touch ${prefix}_nuclei_mask.tif
    touch ${prefix}_cell_mask.tif
    ${ProcessEnvelope.sizeLogStub(task.process, meta.patient_id, "${prefix}.SEGMENT.size.csv")}

    ${ProcessEnvelope.versionsStub(task.process, backend.versionTools, task.container)}
    """
}
