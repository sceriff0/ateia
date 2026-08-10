/*
 * SegBackends - the segmentation backend table.
 *
 * SEGMENT dispatches between three genuinely different segmenters. Before this table
 * existed the choice was re-decided in four places -- a nested `container` ternary, an
 * `if/else if/else` over three near-duplicate script blocks, a per-backend block of flag
 * construction, and again in conf/modules.config -- so adding or retiring a backend meant
 * finding all four and keeping them consistent. Everything that differs between the
 * backends now lives in ONE map, keyed by `params.seg_method`:
 *
 *   container   the image, an immutable tag. Read by SEGMENT's `container` directive,
 *               which is why this is a lib class and not a local in the script block:
 *               directive closures cannot see script-block variables, but they can see
 *               a class on the classpath.
 *   entrypoint  the bin/ script Nextflow stages onto $PATH (tracked executable).
 *   flags       the invocation arguments that are RESOLVED, not configured: an output
 *               prefix, and the nuclear channel index the backend must be pointed at.
 *               Everything TUNABLE lives in conf/modules.config's `ext.args`, per
 *               CLAUDE.md ("tool arguments go in conf/modules.config via ext.args").
 *   guard       the backend's PRECONDITION, as shell lines. These are three different
 *               checks, not boilerplate, and each is meaningless for the other two
 *               backends -- see the per-backend comments below.
 *   versions    the backend-specific rows of versions.yml. StarDist reports
 *               deepcell/tensorflow, InstanSeg instanseg/torch, CellSAM cellSAM/torch.
 *
 * `flags`, `guard` take a context map: [meta: task meta, prefix: output prefix,
 * params: THE SLICE OF params THE BACKENDS READ -- see ctxParams below]. Passing a map
 * rather than pre-extracted values is what keeps each backend's parameter knowledge with
 * that backend instead of leaking back into the process body; passing a SLICE rather than
 * the whole params map is what keeps SEGMENT's cache key narrow.
 *
 * SHELL FRAGMENTS ARE RETURNED AS A LIST OF LINES, never as one pre-indented string.
 * The caller joins them with its own indentation, because Nextflow applies
 * `stripIndent()` to the finished script: a fragment carrying its own leading
 * whitespace would either be stripped inconsistently or flatten the whole block.
 * The lines use single/triple-quoted Groovy strings wherever they contain `$`, so
 * shell expansions such as ${DEEPCELL_ACCESS_TOKEN:-} survive to the shell verbatim.
 */
class SegBackends {

    /**
     * The parameters the backends above read off `ctx.params`, and the only ones SEGMENT
     * is allowed to depend on.
     *
     * WHY A SLICE AND NOT `params`. Nextflow hashes the free variables a process
     * `script:` block references. modules/local/segment.nf used to build its ctx as
     * `[..., params: params]`, so the WHOLE params map entered SEGMENT's task hash and
     * any unrelated parameter change re-ran segmentation and everything downstream of it.
     * Measured: `--pyramid_resolutions 6` alone (a postprocessing knob) re-ran SEGMENT,
     * both property extractors, all five QUANTIFY tasks and both exports.
     *
     * The slice is built HERE, not in the process body, because which parameters a
     * backend needs is backend knowledge -- the whole point of this class, and what
     * tests/test_seg_backends.py's "process body is backend-agnostic" check protects.
     * It is applied in subworkflows/local/segmentation.nf (workflow code is not hashed
     * this way) and arrives at SEGMENT as an opaque value.
     *
     * KEYS ARE STRINGS ON PURPOSE. `params.subMap(...)` never names a parameter as
     * `params.<name>`, so adding a key here does not create a raw parameter read for
     * tests/test_nuclear_marker_routing.py to flag -- `nuclear_markers` still reaches
     * MarkerUtils, which is what that guard is actually about.
     *
     * ADDING A NEW PARAMETER READ TO A BACKEND MEANS ADDING ITS KEY HERE. A missing key reads
     * as null and produces an empty flag with no error, so
     * tests/test_seg_backends_ctx_params.py checks the two lists agree.
     */
    static final List<String> CTX_PARAM_KEYS = [
        'nuclear_markers',
        'instanseg_model_dir',
        'cellsam_model_path',
    ].asImmutable()

    /** The `ctx.params` value SEGMENT should be given. Call from workflow code, never from a `script:` block. */
    static Map ctxParams(Map params) {
        return params.subMap(CTX_PARAM_KEYS)
    }

    private static final Map BACKENDS = [

        // StarDist. Reads ONE channel and is hardcoded to `--dapi-channel 0`, so the
        // precondition is positional: channel 0 must actually BE the nuclear marker.
        // CONVERT_IMAGE reorders channels to make that true; this check is the proof
        // that it happened, and without it a mis-ordered slide is segmented on an
        // arbitrary marker and silently produces plausible-looking nonsense.
        stardist: [
            container : 'bolt3x/attend_image_analysis:segmentation_gpu',
            entrypoint: 'segment.py',
            flags     : { ctx -> '--dapi-channel 0' },
            guard     : { ctx ->
                def channels = ctx.meta.channels ?: []
                def ok = MarkerUtils.isNuclear(channels ? channels[0] : null, ctx.params.nuclear_markers)
                def markers = MarkerUtils.markerList(ctx.params.nuclear_markers).join(', ')
                [
                    '# Runtime validation: channel 0 must be the nuclear marker, because',
                    '# StarDist is invoked with --dapi-channel 0.',
                    "if [ \"${ok}\" != \"true\" ]; then",
                    "    echo \"ERROR: a nuclear channel (${markers}) must be in channel 0 for segmentation\"",
                    "    echo \"Found channels: ${channels ? channels.join(', ') : 'Unknown'}\"",
                    "    echo \"Channel 0: ${channels ? channels[0] : 'Unknown'}\"",
                    '    exit 1',
                    'fi',
                    'echo "Validated: channel 0 is a configured nuclear marker"',
                ]
            },
            versions  : [
                '''deepcell: $(python -c "import deepcell; print(deepcell.__version__)" 2>/dev/null || echo "unknown")''',
                '''tensorflow: $(python -c "import tensorflow; print(tensorflow.__version__)" 2>/dev/null || echo "unknown")''',
            ],
        ],

        // InstanSeg. Channel-invariant -- it consumes the multichannel image directly,
        // so there is no nuclear-channel check to make at all. Its precondition is
        // instead about WRITABILITY: InstanSeg downloads its BioImage.IO model into
        // INSTANSEG_BIOIMAGEIO_PATH, whose default sits inside site-packages, which is
        // read-only under Singularity. Redirect it to the configured host path or, when
        // unconfigured, to the (writable) task work dir, re-downloading per task.
        instantseg: [
            container : 'bolt3x/attend_image_analysis:instant_seg',
            entrypoint: 'segment_instantseg.py',
            flags     : { ctx -> "--prefix ${ctx.prefix}" },
            guard     : { ctx ->
                def cacheDir = ctx.params.instanseg_model_dir ?: '$PWD/.instanseg_cache'
                [
                    '# Redirect the InstanSeg model cache away from the read-only image.',
                    "export INSTANSEG_BIOIMAGEIO_PATH=\"${cacheDir}\"",
                    'mkdir -p "$INSTANSEG_BIOIMAGEIO_PATH"',
                    'echo "InstanSeg model cache: $INSTANSEG_BIOIMAGEIO_PATH"',
                    'echo "Note: InstanSeg is channel-invariant; no nuclear-channel-0 check is enforced."',
                ]
            },
            versions  : [
                '''instanseg: $(python -c "import instanseg; print(getattr(instanseg, '__version__', 'unknown'))" 2>/dev/null || echo "unknown")''',
                '''torch: $(python -c "import torch; print(torch.__version__)" 2>/dev/null || echo "unknown")''',
            ],
        ],

        // CellSAM. Segments ONE nuclear channel like StarDist, but is told which one, so
        // its precondition is existence rather than position: resolve the index from
        // meta.channels and refuse a negative one, because NumPy would wrap -1 around to
        // the LAST channel and segment it without complaint. It also downloads weights
        // when --model-path is unset, which needs DEEPCELL_ACCESS_TOKEN -- warn at the
        // start of the task rather than let a multi-hour job die on the download.
        cellsam: [
            container : 'bolt3x/attend_image_analysis:cellsam',
            entrypoint: 'segment_cellsam.py',
            flags     : { ctx ->
                "--prefix ${ctx.prefix} " +
                "--dapi-channel ${MarkerUtils.nuclearIndex(ctx.meta.channels ?: [], ctx.params.nuclear_markers)}"
            },
            guard     : { ctx ->
                def channels = ctx.meta.channels ?: []
                def idx = MarkerUtils.nuclearIndex(channels, ctx.params.nuclear_markers)
                def markers = MarkerUtils.markerList(ctx.params.nuclear_markers).join(', ')
                def modelPath = ctx.params.cellsam_model_path ?: ''
                [
                    "echo \"Note: CellSAM segments the nuclear channel resolved to index ${idx}; the cell mask is expanded from nuclei.\"",
                    '',
                    '# CellSAM is NOT channel-invariant. Fail loudly when no nuclear channel',
                    '# exists rather than let a negative index wrap around under NumPy.',
                    "if [ \"${idx}\" -lt 0 ]; then",
                    "    echo \"ERROR: no nuclear channel (${markers}) found in [${channels ? channels.join(', ') : 'Unknown'}] for ${ctx.meta.patient_id}\"",
                    '    echo "CellSAM requires a nuclear channel to be present."',
                    '    exit 1',
                    'fi',
                    '',
                    '# Warn early if weight auto-download will be attempted without credentials.',
                    "if [ -z \"${modelPath}\" ] && [ -z \"\${DEEPCELL_ACCESS_TOKEN:-}\" ]; then",
                    '    echo "WARNING: --model-path unset and DEEPCELL_ACCESS_TOKEN is empty; CellSAM weight auto-download will likely fail."',
                    'fi',
                ]
            },
            versions  : [
                '''cellSAM: $(python -c "import cellSAM; print(getattr(cellSAM, '__version__', 'unknown'))" 2>/dev/null || echo "unknown")''',
                '''torch: $(python -c "import torch; print(torch.__version__)" 2>/dev/null || echo "unknown")''',
            ],
        ],
    ]

    /** The backend names this table knows, in declaration order. */
    static List<String> methods() {
        return BACKENDS.keySet() as List
    }

    /**
     * The backend entry for `method`.
     *
     * Throws on an unknown name. The old nested ternary treated anything that was not
     * 'cellsam' or 'instantseg' as StarDist, so a typo (`--seg_method instaseg`) ran a
     * different segmenter than asked for -- either failing later on StarDist's
     * channel-0 check with an error that named the wrong problem, or succeeding with
     * the wrong algorithm. `seg_method` has no schema enum, so this is the only place
     * the typo can be caught.
     */
    static Map of(String method) {
        def backend = BACKENDS[method]
        if (!backend)
            throw new IllegalArgumentException(
                "Unknown seg_method '${method}'. Valid values: ${methods().join(', ')}.")
        return backend
    }

    /** The container image for `method` -- for SEGMENT's `container` directive. */
    static String container(String method) {
        return of(method).container
    }
}
