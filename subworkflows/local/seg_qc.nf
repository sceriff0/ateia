/*
========================================================================================
    SUBWORKFLOW: SEG_QC  (reg_qc = 2 segmentation-overlap QC)
========================================================================================
    Shared by REGISTRATION (linear pipeline) and ADD_CYCLE (incremental cyclic-IF), which
    previously carried near-verbatim copies of this block.

    What it does: segment each slide's DAPI on its NATIVE (pre-registration) image into a
    cell GeoJSON, then warp those polygons through the registrar the registration method
    produced and score BEFORE-vs-AFTER overlap (Dice/IoU/instance-F1) plus per-cell
    centroid residuals.

    The scorer (bin/warp_seg_qc.py) is method-agnostic; only the *warper* differs, so the
    method dispatch lives here rather than at the call sites:
      * valis  -> WARP_SEG_QC       (per-PATIENT transform, BioFormats JVM, pre-micro checkpoint)
      * tiled  -> WARP_SEG_QC_TILED (per-SLIDE transform, JVM-free, no checkpoint)

    The two warp PROCESSES stay separate deliberately. They run in different containers (the
    multi-GB VALIS/BioFormats image vs the ~438 MB JVM-free tiled one) and take differently
    shaped inputs, so merging them would need a conditional `container` directive and
    conditional process inputs — method knowledge pushed back INSIDE a process body, which is
    the thing this seam exists to prevent. Concentrating the dispatch in this one `if` is the
    design, not a leftover.

    `method` is an ARGUMENT, not a read of params.registration_method — the same rule
    REGISTER_PATIENT follows and for the same reason: ADD_CYCLE is VALIS-only by construction
    (workflows/mirage.nf rejects `--registration_method tiled` in add_cycle mode) and passes
    the literal 'valis', so sharing this subworkflow must not be what quietly lifts that
    rejection.

    Whichever transform channel the taken branch does not read arrives as `Channel.empty()`
    — the ADAPTERS' null-object contract (see either adapter's header), not filler invented
    by the caller.

    Input:
        ch_native_images      [meta, file]            slides to segment on their native grid
        ch_transform          [patient_id, transform] per-PATIENT transform (valis branch)
        ch_stage_checkpoint   [patient_id, ckpt_dir]  REGISTER pre-micro checkpoint, may be empty
        ch_transform_by_slide [meta, transform]       per-MOVING-SLIDE transform (tiled branch)
        method                String                  'tiled' | anything else = valis

    Output:
        metrics   [meta, *_seg_qc.json]
        per_cell  [meta, *_reg_residuals.csv]  (optional output of the warp process)
        size_log  size logs of SEG_QC_GEOJSON + whichever warp process ran
        versions  versions.yml of SEG_QC_GEOJSON + whichever warp process ran (already .first())
========================================================================================
*/

include { SEG_QC_GEOJSON    } from '../../modules/local/seg_qc_geojson'
include { WARP_SEG_QC       } from '../../modules/local/warp_seg_qc'
include { WARP_SEG_QC_TILED } from '../../modules/local/warp_seg_qc_tiled'

workflow SEG_QC {
    take:
    ch_native_images
    ch_transform
    ch_stage_checkpoint
    ch_transform_by_slide
    method

    main:
    SEG_QC_GEOJSON(ch_native_images)

    ch_gj = SEG_QC_GEOJSON.out.geojson.branch { meta, gj ->
        reference: meta.is_reference
        moving:    !meta.is_reference
    }
    // reference: [patient_id, ref_geojson, ref_slide_name(=stem)]
    ch_ref_gj = ch_gj.reference.map { meta, gj -> [meta.patient_id, gj, gj.simpleName] }
    // moving: [patient_id, meta, moving_geojson, moving_slide_name]
    ch_mov_gj = ch_gj.moving.map { meta, gj -> [meta.patient_id, meta, gj, gj.simpleName] }

    if (method == 'tiled') {
        // Tiled: one transform per moving slide (meta-keyed). Join it to the moving GeoJSON by
        // (patient, sorted-channels) so each slide is scored against its own transform. No
        // stage checkpoint (stages are separable by construction) and no JVM.
        ch_manifest_keyed = ch_transform_by_slide
            .map { meta, m -> [meta.patient_id, meta.channels.toSorted().join('|'), m] }

        ch_for_warp_tiled = ch_mov_gj
            .map { pid, meta, gj, name -> [pid, meta.channels.toSorted().join('|'), meta, gj, name] }
            .combine(ch_ref_gj, by: 0)
            .combine(ch_manifest_keyed, by: [0, 1])
            .map { _pid, _sig, meta, mov_gj, mov_name, ref_gj, ref_name, m ->
                tuple(meta, m, ref_name, mov_name, ref_gj, mov_gj)
            }

        WARP_SEG_QC_TILED(ch_for_warp_tiled)
        ch_metrics      = WARP_SEG_QC_TILED.out.metrics
        ch_per_cell     = WARP_SEG_QC_TILED.out.per_cell
        ch_warp_size    = WARP_SEG_QC_TILED.out.size_log
        ch_warp_versions = WARP_SEG_QC_TILED.out.versions
    } else {
        // VALIS: one transform (the registrar pickle) per patient. Exactly one stage-checkpoint
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
                tuple(meta, pickle, ref_name, mov_name, ref_gj, mov_gj, ckpt)
            }

        WARP_SEG_QC(ch_for_warp)
        ch_metrics       = WARP_SEG_QC.out.metrics
        ch_per_cell      = WARP_SEG_QC.out.per_cell
        ch_warp_size     = WARP_SEG_QC.out.size_log
        ch_warp_versions = WARP_SEG_QC.out.versions
    }

    emit:
    metrics  = ch_metrics
    per_cell = ch_per_cell
    size_log = SEG_QC_GEOJSON.out.size_log.mix(ch_warp_size)
    versions = SEG_QC_GEOJSON.out.versions.first().mix(ch_warp_versions.first())
}
