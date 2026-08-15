/*
========================================================================================
    SUBWORKFLOW: SEG_QC  (reg_qc = 2 segmentation-overlap QC)
========================================================================================
    Shared by REGISTRATION (linear pipeline) and ADD_CYCLE (incremental cyclic-IF), which
    previously carried near-verbatim copies of this block.

    What it does: segment each slide on its NATIVE (pre-registration) image, trace the
    resulting cell mask into a GeoJSON, then warp those polygons through the registrar the
    registration method produced and score BEFORE-vs-AFTER overlap (per-pair IoU, dice_matched)
    plus per-cell centroid residuals.

    THE QC SEGMENTS WITH THE RUN'S OWN SEGMENTER. SEG_QC_SEGMENT is `SEGMENT` under an
    alias, so `params.seg_method` selects the backend here exactly as it does for the
    shipped masks. Until this change SEG_QC_GEOJSON ran StarDist unconditionally, which
    (a) scored a different segmenter's cells than the run shipped at the default
    seg_method='instantseg', and (b) could not run at all at stock defaults -- see that
    module's header. Segmenting the clean NATIVE image rather than a warped one is
    unchanged and still the point: it isolates registration quality from
    segment-on-interpolated-pixels bias.

    The scorer (bin/warp_seg_qc.py) is method-agnostic; only the *warper* differs, so the
    dispatch lives here rather than at the call sites:
      * a per-PATIENT transform  -> combine by patient_id; BioFormats JVM; pre-micro checkpoint
      * a per-SLIDE   transform  -> combine by (patient_id, channel signature); JVM-free

    ONE WARP PROCESS, TWO BACKENDS. WARP_SEG_QC's container, flags, stage list, version
    rows and stub extras come from lib/WarpBackends.groovy, keyed on the `method`
    argument. This file's `if` shapes the input tuple — the two joins genuinely differ,
    because tiled has one transform per moving slide and VALIS one registrar per patient
    — and then calls the one process.

    This reverses an earlier decision recorded here, which argued the two processes should
    stay separate because merging would need a conditional container directive and
    conditional inputs. modules/local/segment.nf does exactly that for three segmentation
    backends and ships green; holding both positions cost more than either. The absent
    stage checkpoint is a null object (`[]`), which is the idiom the VALIS arm already
    used for patients whose checkpoint was missing.

    WHICH JOIN IS CHOSEN BY THE DECLARED CARDINALITY, NOT BY THE BACKEND'S NAME. This file
    used to read `if (method == 'tiled')`. That made the join shape depend on a product
    name travelling out of band from params.registration_method, while the fact it was
    really standing in for is the multiplicity of `transform` — and the two are only
    accidentally the same thing. A third backend filling `transform` per slide, whose
    author did not also find and edit this `if`, would have been combined by patient_id
    alone; `combine(by:)` is a CROSS JOIN, so N moving slides against N transforms is
    N**2 pairs, each slide scored against every transform of its patient. Silently wrong
    QC numbers, not a crash. lib/AdapterContract.groovy declares the cardinality and this
    file asks it, so a new backend's join follows from its declaration.

    `contract` is an ARGUMENT here — never a read of params.registration_method, which is
    read exactly once, in subworkflows/local/registration.nf. That is the same rule
    REGISTER_PATIENT follows and for the same reason: ADD_CYCLE is VALIS-only by
    construction (workflows/mirage.nf rejects `--registration_method tiled` in add_cycle
    mode) and passes AdapterContract.of('valis'), so sharing this subworkflow must not be
    what quietly lifts that rejection. WARP_SEG_QC still takes the backend NAME
    (`contract.method`): it selects a container and a scorer flag, which is a genuine
    name-to-implementation lookup (lib/WarpBackends.groovy) rather than a shape decision.

    Whichever transform channel the taken branch does not read arrives as `Channel.empty()`
    — the ADAPTERS' null-object contract (see either adapter's header), not filler invented
    by the caller.

    Input:
        ch_native_images      [meta, file]            slides to segment on their native grid
        ch_transform          [patient_id, transform] per-PATIENT transform (valis branch)
        ch_stage_checkpoint   [patient_id, ckpt_dir]  REGISTER pre-micro checkpoint, may be empty
        ch_transform_by_slide [meta, transform]       per-MOVING-SLIDE transform (per-slide branch)
        contract              Map                     AdapterContract.of(method) — the running
                                                      backend's declared emit cardinalities

    Output:
        metrics   [meta, *_seg_qc.json]
        per_cell  [meta, *_reg_residuals.csv]  (optional output of the warp process)
        size_log  size logs of SEG_QC_SEGMENT + SEG_QC_GEOJSON + whichever warp process ran
        versions  versions.yml of SEG_QC_SEGMENT + SEG_QC_GEOJSON + whichever warp process
                  ran (already .first())
========================================================================================
*/

include { SEGMENT as SEG_QC_SEGMENT } from '../../modules/local/segment'
include { SEG_QC_GEOJSON            } from '../../modules/local/seg_qc_geojson'
include { WARP_SEG_QC               } from '../../modules/local/warp_seg_qc'

workflow SEG_QC {
    take:
    ch_native_images
    ch_transform
    ch_stage_checkpoint
    ch_transform_by_slide
    contract

    main:
    // ── segment the native slides with THE RUN'S OWN SEGMENTER ──────────────────
    // SEGMENT itself, aliased. Not a second segmentation code path that happens to
    // agree with it: the alias is the same process body, the same lib/SegBackends
    // table, the same container/guard/flags, so the QC follows params.seg_method for
    // free and cannot drift from what the run actually ships. Before this, SEG_QC_GEOJSON
    // ran StarDist unconditionally -- see that module's header for the two bugs that
    // caused, including a silent no-op at stock defaults.
    //
    // ALIASING INHERITS CONFIG, INCLUDING publishDir -- which is the trap here. Nextflow
    // matches a `withName:` selector against an alias' own name AND its process' original
    // declared name, so conf/modules.config's `withName: 'SEGMENT'` already governs
    // SEG_QC_SEGMENT (container, resources, backend ext.args, GPU wiring -- all of which we
    // want) and would also publish these masks into <pid>/segmentation/, a tree
    // csv/segmented.csv indexes and which must contain exactly one per-patient mask. A
    // `withName: 'SEG_QC_SEGMENT'` block, textually after SEGMENT's, turns publishing off
    // and sets a per-slide ext.prefix. This is the same shape, and the same hazard, as
    // SPLIT_CHANNELS / SPLIT_PRIOR_PYRAMID -- see that pair's comment in
    // conf/modules.config for the debug-log evidence. tests/test_process_alias_config.py
    // fails if an aliased process ever loses that override.
    def seg_params = SegBackends.ctxParams(params)

    // The slide name is resolved ONCE, here, from the native image, and travels as
    // meta.qc_slide. It must equal the registrar's slide_dict key -- VALIS's own
    // convention (valtils.get_name strips .ome.tif/.ome.tiff) -- because WARP_SEG_QC looks
    // the slide up by it. It cannot be recovered downstream from the mask's filename: the
    // stardist backend names masks after the image stem ('foo.ome'), instantseg and cellsam
    // after ext.prefix. Carrying it on meta rather than joining it back keeps the pairing
    // structural instead of dependent on Map equality holding across a process boundary.
    ch_to_segment = ch_native_images.map { meta, img ->
        tuple(meta + [qc_slide: img.name.replaceAll(/\.ome\.tiff?$/, '').replaceAll(/\.tiff?$/, '')],
              img,
              seg_params)
    }
    SEG_QC_SEGMENT(ch_to_segment)

    // Label image -> polygons. Backend-agnostic by construction: it reads a plain label
    // TIFF, which all three backends emit under the same `*_cell_mask.tif` contract.
    SEG_QC_GEOJSON(SEG_QC_SEGMENT.out.cell_mask)

    ch_gj = SEG_QC_GEOJSON.out.geojson.branch { meta, gj ->
        reference: meta.is_reference
        moving:    !meta.is_reference
    }
    // reference: [patient_id, ref_geojson, ref_slide_name(=stem)]
    ch_ref_gj = ch_gj.reference.map { meta, gj -> [meta.patient_id, gj, gj.simpleName] }
    // moving: [patient_id, meta, moving_geojson, moving_slide_name]
    ch_mov_gj = ch_gj.moving.map { meta, gj -> [meta.patient_id, meta, gj, gj.simpleName] }

    // Both arms shape the same 8-element tuple WARP_SEG_QC takes —
    // [meta, method, transform, ref_slide, moving_slide, ref_geojson, moving_geojson,
    // stage_checkpoint] — and differ only in how `transform` and `stage_checkpoint` are
    // joined, because the two methods' transforms are keyed differently (per-slide vs
    // per-patient) and only VALIS has a stage checkpoint at all.
    def method = contract.method
    if (AdapterContract.isPerSlide(contract, 'transform')) {
        // A per-slide transform (meta-keyed) — join it to the moving GeoJSON by
        // (patient, sorted-channels) so each slide is scored against its own transform. No
        // stage checkpoint (a backend with a per-slide transform composes no stages
        // destructively) and no JVM — `[]` is the null object this backend never reads.
        ch_manifest_keyed = ch_transform_by_slide
            .map { meta, m -> [meta.patient_id, meta.channels.toSorted().join('|'), m] }

        ch_for_warp = ch_mov_gj
            .map { pid, meta, gj, name -> [pid, meta.channels.toSorted().join('|'), meta, gj, name] }
            .combine(ch_ref_gj, by: 0)
            .combine(ch_manifest_keyed, by: [0, 1])
            .map { _pid, _sig, meta, mov_gj, mov_name, ref_gj, ref_name, m ->
                tuple(meta, method, m, ref_name, mov_name, ref_gj, mov_gj, [])
            }
    } else {
        // A per-patient transform (VALIS's registrar pickle). Exactly one stage-checkpoint
        // entry per patient that has one — the real directory where REGISTER wrote it, `[]`
        // where it did not. Making it total matters: a plain combine on an optional channel
        // silently DROPS the patients that lack it, removing them from the QC instead of
        // costing a stage.
        ch_ckpt_by_patient = ch_transform
            .map { pid, _pickle -> tuple(pid, []) }
            .join(ch_stage_checkpoint, by: 0, remainder: true)
            .map { pid, _placeholder, ckpt -> tuple(pid, ckpt ?: []) }

        // Join each moving slide with its patient's reference GeoJSON, transform and
        // stage checkpoint.
        ch_for_warp = ch_mov_gj
            .combine(ch_ref_gj, by: 0)
            .combine(ch_transform, by: 0)
            .combine(ch_ckpt_by_patient, by: 0)
            .map { pid, meta, mov_gj, mov_name, ref_gj, ref_name, pickle, ckpt ->
                tuple(meta, method, pickle, ref_name, mov_name, ref_gj, mov_gj, ckpt)
            }
    }

    WARP_SEG_QC(ch_for_warp)
    ch_metrics       = WARP_SEG_QC.out.metrics
    ch_per_cell      = WARP_SEG_QC.out.per_cell
    ch_warp_size     = WARP_SEG_QC.out.size_log
    ch_warp_versions = WARP_SEG_QC.out.versions

    emit:
    metrics  = ch_metrics
    per_cell = ch_per_cell
    size_log = SEG_QC_SEGMENT.out.size_log.mix(SEG_QC_GEOJSON.out.size_log).mix(ch_warp_size)
    versions = SEG_QC_SEGMENT.out.versions.first()
        .mix(SEG_QC_GEOJSON.out.versions.first())
        .mix(ch_warp_versions.first())
}
