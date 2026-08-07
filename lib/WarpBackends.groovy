/*
========================================================================================
    WarpBackends — per-method knobs for the reg_qc=2 segmentation-overlap warp
========================================================================================
    WARP_SEG_QC scores registration by warping reference and moving native-cell GeoJSONs
    through each registration stage and comparing per-pair IoU and centroid residual
    against a correspondence fixed at the rigid anchor. The SCORER is method-agnostic —
    bin/warp_seg_qc.py takes `--method` and builds its warper from either a VALIS
    registrar pickle or a STARE transform manifest.

    The two PROCESSES were not. modules/local/warp_seg_qc.nf and warp_seg_qc_tiled.nf
    shared ~77% of their bodies — the whole output: block including its comment, the
    when: block, both tag lines, 8 of 12 flags, 7 of 9 stub-JSON keys — and differed only
    in container, one input element, four flags, the stage list and two stub keys.

    WHY THIS REVERSES A RECORDED DECISION. seg_qc.nf's header argued the split was
    deliberate: merging would need a conditional container directive and conditional
    inputs, "method knowledge pushed back INSIDE a process body". That argument was sound
    when written, but modules/local/segment.nf now does exactly that for three
    segmentation backends via lib/SegBackends.groovy, and shipped green. Holding both
    positions — one table for SEGMENT, two processes for WARP_SEG_QC — costs more than
    either position alone. This class is SegBackends' shape applied to the same problem.

    THE ABSENT STAGE CHECKPOINT IS A NULL OBJECT, NOT A DIFFERENT INPUT SHAPE. The VALIS
    branch already passes `[]` for patients whose checkpoint is missing (seg_qc.nf's
    ch_ckpt_by_patient makes that join total precisely so a missing checkpoint costs a
    stage rather than dropping the patient). The tiled method needs no checkpoint at all,
    so it passes `[]` for every slide. One 7-element input tuple serves both.

    SHELL FRAGMENTS ARE RETURNED AS A LIST OF LINES, never pre-indented — the caller
    joins them with its own indentation, because Nextflow applies stripIndent() to the
    finished script. Same rule as SegBackends.

    All methods are static; nothing here reads params. `method` arrives as an argument,
    which is what keeps params.registration_method read exactly once on the linear path
    (in subworkflows/local/registration.nf).
========================================================================================
*/

class WarpBackends {

    private static final Map<String, Map> BACKENDS = [
        valis: [
            // Same image as REGISTER's classic path, so the registrar pickle loads and
            // scikit-image/scipy are present.
            container   : 'cdgatenbee/valis-wsi:1.0.0',
            stages      : ['native', 'rigid', 'non_rigid', 'micro'],
            versionTools: ['valis', 'skimage', 'scipy'],
            flags       : { ctx ->
                // VALIS composes the micro residual into the same displacement field, so
                // without the pre-micro checkpoint 'non_rigid' cannot be separated from
                // 'micro'. Optional but load-bearing; absent as `[]`.
                def out = [
                    "--moving-name '${ctx.moving_slide}'",
                    "--reference-name '${ctx.ref_slide}'",
                    "--micro-reg ${ctx.micro_reg}",
                ]
                if (ctx.stage_checkpoint) out << "--checkpoint-dir ${ctx.stage_checkpoint}"
                return out
            },
            stubExtras  : { ctx -> [
                micro_reg                 : ctx.micro_reg,
                rigid_includes_micro_rigid: ctx.micro_reg >= 1,
            ] },
        ],
        tiled: [
            // JVM-free slim image; no BioFormats.
            container   : 'bolt3x/attend_image_analysis:tiled',
            stages      : ['native', 'rigid', 'refined'],
            versionTools: ['skimage', 'scipy'],
            flags       : { _ctx -> ['--method tiled'] },
            stubExtras  : { _ctx -> [:] },
        ],
    ].asImmutable()

    static List<String> methods() {
        return BACKENDS.keySet() as List
    }

    static Map of(String method) {
        def backend = BACKENDS[method]
        if (!backend)
            throw new IllegalArgumentException(
                "Unknown registration method for WARP_SEG_QC: '${method}'. Valid: ${methods()}")
        return backend
    }

    /** Read by WARP_SEG_QC's container directive. */
    static String container(String method) {
        return of(method).container
    }
}
