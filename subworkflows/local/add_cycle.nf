/*
========================================================================================
    SUBWORKFLOW: ADD_CYCLE  (incremental cyclic-IF)
========================================================================================
    Folds a NEW imaging cycle into a prior completed patient result. Reuses the
    prior reference (registration target), the prior segmentation cell + nuclei
    masks (no SEGMENT), and the prior merged quantification table (base).
    Recomputes only: preprocessing + registration of the new slide, quantification
    of the new markers, and a wholesale regenerate of cells.geojson + the pyramid
    over the COMBINED channel/measurement set.

    Assumes true cyclic IF: the same physical section re-imaged, so the prior
    masks' cell labels align with the newly registered cycle. Registration-drift
    QC surfaces misalignment; it is non-gating:
      - reg_qc >= 1: DAPI-overlay image QC (GENERATE_REGISTRATION_QC)
      - reg_qc >= 2: + staged seg-overlap QC (SEG_QC_GEOJSON -> WARP_SEG_QC) — per-pair
        IoU and centroid residual at each registration stage, on a correspondence fixed
        after rigid. Uses the classic VALIS registrar pickle from the 2-node graph, plus
        REGISTER's pre-micro stage checkpoint. See docs/registration_qc.md.

    Registration adapter: the classic monolithic VALIS adapter, matching a full run.
    (The distributed/tiled low-memory path was archived 2026-07-24; see git tag
    archive/tiled-valis-2026-07-24.)

    Per-compartment (expanded) quantification is supported: when
    params.quantify_compartments, the reused nuclei mask feeds QUANTIFY and
    EXTRACT_NUCLEI_PROPERTIES supplies nucleus contours for EXPORT_GEOJSON.
========================================================================================
*/

include { PREPROCESSING            } from './preprocess'
include { VALIS_ADAPTER            } from './adapters/valis_adapter'
include { EXTRACT_CELL_PROPERTIES  } from '../../modules/local/extract_cell_properties'
include { EXTRACT_NUCLEI_PROPERTIES } from '../../modules/local/extract_nuclei_properties'
include { SPLIT_CHANNELS           } from '../../modules/local/split_channels'
include { SPLIT_CHANNELS as SPLIT_PRIOR_PYRAMID } from '../../modules/local/split_channels'
include { QUANTIFY                 } from '../../modules/local/quantify'
include { MERGE_QUANT_CSVS         } from '../../modules/local/quantify'
include { EXPORT_GEOJSON           } from '../../modules/local/export_geojson'
include { MERGE_AND_PYRAMID        } from '../../modules/local/merge_and_pyramid'
include { GENERATE_REGISTRATION_QC } from '../../modules/local/generate_registration_qc'
include { SEG_QC_GEOJSON           } from '../../modules/local/seg_qc_geojson'
include { WARP_SEG_QC              } from '../../modules/local/warp_seg_qc'

workflow ADD_CYCLE {
    take:
    ch_new_input     // [meta, raw_file]  (new cycle slides, is_reference=false)
    ch_prior_assets  // [patient_id, ref_channels(List), ref_image, base_merged_csv, cell_mask, nuclei_mask, pyramid]

    main:
    // ------------------------------------------------------------------ //
    // 1. PREPROCESS the new cycle (mirrors cycle-1 illumination/prep)
    // ------------------------------------------------------------------ //
    PREPROCESSING(ch_new_input)
    ch_new_pre = PREPROCESSING.out.preprocessed   // [meta, file]

    // ------------------------------------------------------------------ //
    // 2. REGISTER new cycle -> frozen prior reference (2-node VALIS graph)
    // ------------------------------------------------------------------ //
    // Build [patient_id, ref_item, all_items] with the prior reference marked
    // is_reference=true. The reference is a fixed frame (passed through
    // unregistered by VALIS); we discard its output and keep only the new cycle.
    ch_ref_item = ch_prior_assets.map { pid, ref_channels, ref_image, _m, _cm, _nm, _py ->
        def ref_meta = [patient_id: pid, id: "${pid}_reference", is_reference: true, channels: ref_channels]
        [pid, [ref_meta, ref_image]]
    }
    ch_grouped = ch_new_pre
        .map { meta, file -> [meta.patient_id, [meta + [is_reference: false], file]] }
        .combine(ch_ref_item, by: 0)
        .map { pid, new_item, ref_item ->
            [pid, ref_item, [ref_item, new_item]]   // all_items = reference + new cycle
        }

    // Registration runs via the classic monolithic VALIS adapter, matching a full run.
    // The distributed/tiled low-memory path was archived on 2026-07-24 (git tag
    // archive/tiled-valis-2026-07-24).
    ch_registrar = Channel.empty()

    VALIS_ADAPTER(ch_grouped)
    ch_adapter_registered = VALIS_ADAPTER.out.registered
    ch_adapter_versions   = VALIS_ADAPTER.out.versions
    ch_registrar          = VALIS_ADAPTER.out.registrar

    // Keep only the newly registered cycle (drop the reference passthrough).
    ch_new_registered = ch_adapter_registered.filter { meta, _f -> !meta.is_reference }

    // ------------------------------------------------------------------ //
    // 3. REGISTRATION-DRIFT QC (non-gating)
    // ------------------------------------------------------------------ //
    ch_qc     = Channel.empty()
    ch_seg_qc = Channel.empty()
    def reg_qc_level = ParamUtils.regQcLevel(params)

    // Level 2 needs the classic registrar pickle, which the classic VALIS adapter produces.
    def do_seg_qc = reg_qc_level >= 2

    // Level >= 1: DAPI-overlay image QC — new registered vs prior reference.
    if (reg_qc_level >= 1) {
        ch_for_qc = ch_new_registered
            .map { meta, f -> [meta.patient_id, meta, f] }
            .combine(ch_prior_assets.map { pid, _rc, ref_image, _m, _cm, _nm, _py -> [pid, ref_image] }, by: 0)
            .map { _pid, meta, reg_f, ref_f -> [meta, reg_f, ref_f] }
        GENERATE_REGISTRATION_QC(ch_for_qc)
        ch_qc = GENERATE_REGISTRATION_QC.out.qc
    }

    // Level >= 2: seg-overlap Dice/IoU. Segment DAPI on the NATIVE (pre-reg)
    // reference + new-cycle images -> cell GeoJSON, then warp through the
    // classic registrar pickle (classic adapter only — see do_seg_qc above).
    // Mirrors registration.nf's seg-QC block.
    if (do_seg_qc) {
        ch_native = ch_new_pre
            .map { meta, f -> [meta + [is_reference: false], f] }
            .mix(ch_prior_assets.map { pid, rc, ref_image, _m, _cm, _nm, _py ->
                [[patient_id: pid, id: "${pid}_reference", is_reference: true, channels: rc], ref_image]
            })
        SEG_QC_GEOJSON(ch_native)

        ch_gj = SEG_QC_GEOJSON.out.geojson.branch { meta, gj ->
            reference: meta.is_reference
            moving:    !meta.is_reference
        }
        ch_ref_gj = ch_gj.reference.map { meta, gj -> [meta.patient_id, gj, gj.simpleName] }
        ch_mov_gj = ch_gj.moving.map    { meta, gj -> [meta.patient_id, meta, gj, gj.simpleName] }

        // One stage-checkpoint entry per patient, `[]` where REGISTER wrote none — see the
        // same construction in registration.nf. Total on purpose: combining on an optional
        // channel would drop the patients that lack it rather than costing them a stage.
        ch_ckpt_by_patient = VALIS_ADAPTER.out.registrar
            .map { pid, _pickle -> tuple(pid, []) }
            .join(VALIS_ADAPTER.out.stage_checkpoint, by: 0, remainder: true)
            .map { pid, _placeholder, ckpt -> tuple(pid, ckpt ?: []) }

        ch_for_warp = ch_mov_gj
            .combine(ch_ref_gj, by: 0)
            .combine(ch_registrar, by: 0)
            .combine(ch_ckpt_by_patient, by: 0)
            .map { pid, meta, mov_gj, mov_name, ref_gj, ref_name, pickle, ckpt ->
                tuple(meta, pickle, ref_name, mov_name, ref_gj, mov_gj, ckpt)
            }
        WARP_SEG_QC(ch_for_warp)
        ch_seg_qc = WARP_SEG_QC.out.metrics
    }

    // ------------------------------------------------------------------ //
    // 4. REUSE prior masks -> cell contours (+ nucleus contours if compartments)
    //    Masks are unchanged, so cell labels are identical.
    // ------------------------------------------------------------------ //
    ch_prior_cell_mask = ch_prior_assets.map { pid, _rc, _ri, _m, cell_mask, _nm, _py ->
        [[patient_id: pid, id: pid, is_reference: true], cell_mask]
    }
    EXTRACT_CELL_PROPERTIES(ch_prior_cell_mask)
    ch_contours = EXTRACT_CELL_PROPERTIES.out.contours.map { meta, j -> [meta.patient_id, j] }

    // Nucleus contours (re-keyed to cell labels) — only when compartments enabled.
    ch_nucleus_contours = Channel.empty()
    if (params.quantify_compartments) {
        ch_nuclei_props_in = ch_prior_assets.map { pid, _rc, _ri, _m, cell_mask, nuclei_mask, _py ->
            [[patient_id: pid, id: pid], nuclei_mask, cell_mask]
        }
        EXTRACT_NUCLEI_PROPERTIES(ch_nuclei_props_in)
        ch_nucleus_contours = EXTRACT_NUCLEI_PROPERTIES.out.contours.map { meta, j -> [meta.patient_id, j] }
    }

    // ------------------------------------------------------------------ //
    // 5. SPLIT new registered cycle -> per-marker single-channel TIFFs
    //    (SPLIT_CHANNELS drops the new cycle's DAPI: non-reference input.)
    // ------------------------------------------------------------------ //
    SPLIT_CHANNELS(ch_new_registered.map { meta, f -> [meta, f, false] })

    ch_new_channels = SPLIT_CHANNELS.out.channels
        .flatMap { meta, tiffs ->
            // Map addition (+) returns a new map; clone() would only
            // shallow-copy and alias meta.channels across every derived m.
            (tiffs instanceof List ? tiffs : [tiffs]).collect { tiff ->
                def m = meta + [
                    id: "${meta.patient_id}_${tiff.baseName}",
                    channel_name: tiff.baseName
                ]
                [m, tiff]
            }
        }

    // ------------------------------------------------------------------ //
    // 6. QUANTIFY new markers against the REUSED masks. QUANTIFY reads
    //    params.quantify_compartments to route the nuclei mask; --expanded
    //    arrives via ext.args (conf/modules.config), so no change here.
    // ------------------------------------------------------------------ //
    ch_masks = ch_prior_assets.map { pid, _rc, _ri, _m, cell_mask, nuclei_mask, _py ->
        [pid, cell_mask, nuclei_mask]
    }
    ch_for_quant = ch_new_channels
        .map { meta, tiff -> [meta.patient_id, meta, tiff] }
        .combine(ch_masks, by: 0)
        .map { _pid, meta, tiff, cell_mask, nuclei_mask -> [meta, tiff, cell_mask, nuclei_mask] }
    QUANTIFY(ch_for_quant)

    // ------------------------------------------------------------------ //
    // 7. MERGE new marker CSVs onto the prior merged table (base) by label
    // ------------------------------------------------------------------ //
    ch_new_quant_grouped = QUANTIFY.out.individual_csv
        .map { meta, csv -> [meta.patient_id, csv] }
        .groupTuple()   // all new-marker CSVs for the patient
    ch_base = ch_prior_assets.map { pid, _rc, _ri, base_csv, _cm, _nm, _py -> [pid, base_csv] }
    ch_for_merge = ch_new_quant_grouped
        .combine(ch_base, by: 0)
        .map { pid, csvs, base_csv -> [[patient_id: pid, id: pid], csvs, base_csv] }
    MERGE_QUANT_CSVS(ch_for_merge)

    // ------------------------------------------------------------------ //
    // 8. EXPORT complete cells.geojson from the COMBINED table.
    //    nucleus slot: real nucleus contours when compartments enabled,
    //    else the cell contours (harmless placeholder — EXPORT_GEOJSON only
    //    passes --nucleus_contours_json under params.quantify_compartments).
    //    add_cycle does not run phenotyping (no COMPILE_PANEL/PHENOTYPE here):
    //    the phenotypes/model_config slots reuse the cell contours file as a
    //    harmless placeholder. panel_spec/panel_model are REJECTED at launch in
    //    add_cycle mode (ParamUtils.validateAddCyclePhenotyping), so EXPORT_GEOJSON's
    //    arg guard is always off here and the legacy constant-"Cell" export is kept.
    // ------------------------------------------------------------------ //
    ch_nuc_for_export = params.quantify_compartments ? ch_nucleus_contours : ch_contours
    ch_for_export = MERGE_QUANT_CSVS.out.merged_csv
        .map { meta, csv -> [meta.patient_id, meta, csv] }
        .join(ch_contours, by: 0)
        .join(ch_nuc_for_export, by: 0)
        .map { _pid, meta, csv, contours, nucleus_contours ->
            [meta, csv, contours, nucleus_contours, contours, contours]
        }
    EXPORT_GEOJSON(ch_for_export)

    // ------------------------------------------------------------------ //
    // 9. REBUILD complete pyramid: recover prior channels from the prior
    //    pyramid (is_reference=true keeps ref DAPI + all old markers), then
    //    merge with the new cycle's channels.
    // ------------------------------------------------------------------ //
    ch_prior_pyramid = ch_prior_assets.map { pid, _rc, _ri, _m, _cm, _nm, pyramid ->
        [[patient_id: pid, id: pid, is_reference: true, channels: []], pyramid, true]
    }
    SPLIT_PRIOR_PYRAMID(ch_prior_pyramid)

    // Deterministic new-wins dedup on a marker-name collision (matches the
    // quantification merge's new-wins rule). Tag new-cycle channels priority 0
    // and prior-pyramid channels priority 1; group by [pid, marker] and keep the
    // lowest priority. An async .mix + first-occurrence .unique would be
    // scheduling-nondeterministic, and could make the pyramid show the OLD image
    // of a re-imaged marker while the quant CSV correctly shows the new one.
    // (New cycle excludes DAPI via SPLIT_CHANNELS, so DAPI only ever comes from
    // the prior pyramid at priority 1 — the reference DAPI is preserved.)
    ch_new_tagged = SPLIT_CHANNELS.out.channels.flatMap { meta, tiffs ->
        (tiffs instanceof List ? tiffs : [tiffs]).collect { tiff -> [[meta.patient_id, tiff.baseName], 0, tiff] }
    }
    ch_prior_tagged = SPLIT_PRIOR_PYRAMID.out.channels.flatMap { meta, tiffs ->
        (tiffs instanceof List ? tiffs : [tiffs]).collect { tiff -> [[meta.patient_id, tiff.baseName], 1, tiff] }
    }
    ch_all_channels = ch_new_tagged.mix(ch_prior_tagged)
        .groupTuple(by: 0)   // [[pid, marker], [priorities...], [tiffs...]]
        .map { key, prios, tiffs ->
            def winner = [prios, tiffs].transpose().sort { a, b -> a[0] <=> b[0] }.first()
            [key[0], winner[1]]   // [pid, winning_tiff] — lowest priority (new) wins
        }
        .groupTuple()
        .map { pid, tiffs -> [[patient_id: pid, id: pid, is_reference: false], tiffs] }

    // MERGE_AND_PYRAMID now takes a 3-tuple [meta, split_channels, mask_files]
    // (mask embedding as a second OME series — see merge_and_pyramid.nf and
    // postprocess.nf's ch_pyramid_in). ADD_CYCLE mirrors postprocess.nf's
    // embed_masks gate: reuse the SAME cell + nuclei masks from ch_prior_assets
    // (they are unchanged in ADD_CYCLE — no re-derivation), embedding them into
    // the rebuilt combined pyramid only when embed_masks is on. Otherwise pass
    // an empty mask list so Nextflow stages no masks/ dir.
    def emit_masks = params.embed_masks && params.quantify_compartments && params.expanded_quantification
    ch_masks_for_pyramid = ch_prior_assets.map { pid, _rc, _ri, _m, cell_mask, nuclei_mask, _py -> [pid, cell_mask, nuclei_mask] }
    ch_pyramid_in = emit_masks
        ? ch_all_channels
            .map { meta, tiffs -> [meta.patient_id, meta, tiffs] }
            .combine(ch_masks_for_pyramid, by: 0)
            .map { _pid, meta, tiffs, cm, nm -> [meta, tiffs, [cm, nm]] }
        : ch_all_channels.map { meta, tiffs -> [meta, tiffs, []] }
    MERGE_AND_PYRAMID(ch_pyramid_in)

    // ------------------------------------------------------------------ //
    // Versions + size logs
    // ------------------------------------------------------------------ //
    ch_versions = Channel.empty()
        .mix(PREPROCESSING.out.versions)
        .mix(ch_adapter_versions)
        .mix(EXTRACT_CELL_PROPERTIES.out.versions.first())
        .mix(SPLIT_CHANNELS.out.versions.first())
        .mix(SPLIT_PRIOR_PYRAMID.out.versions.first())
        .mix(QUANTIFY.out.versions.first())
        .mix(MERGE_QUANT_CSVS.out.versions.first())
        .mix(EXPORT_GEOJSON.out.versions.first())
        .mix(MERGE_AND_PYRAMID.out.versions.first())

    ch_size_logs = Channel.empty()
        .mix(SPLIT_CHANNELS.out.size_log)
        .mix(SPLIT_PRIOR_PYRAMID.out.size_log)
        .mix(QUANTIFY.out.size_log)
        .mix(MERGE_QUANT_CSVS.out.size_log)
        .mix(EXPORT_GEOJSON.out.size_log)
        .mix(MERGE_AND_PYRAMID.out.size_log)
        .mix(EXTRACT_CELL_PROPERTIES.out.size_log)

    if (reg_qc_level >= 1) {
        ch_versions  = ch_versions.mix(GENERATE_REGISTRATION_QC.out.versions.first())
        ch_size_logs = ch_size_logs.mix(GENERATE_REGISTRATION_QC.out.size_log)
    }
    if (do_seg_qc) {
        ch_versions  = ch_versions.mix(SEG_QC_GEOJSON.out.versions.first()).mix(WARP_SEG_QC.out.versions.first())
        ch_size_logs = ch_size_logs.mix(SEG_QC_GEOJSON.out.size_log).mix(WARP_SEG_QC.out.size_log)
    }
    if (params.quantify_compartments) {
        ch_versions  = ch_versions.mix(EXTRACT_NUCLEI_PROPERTIES.out.versions.first())
        ch_size_logs = ch_size_logs.mix(EXTRACT_NUCLEI_PROPERTIES.out.size_log)
    }

    emit:
    geojson     = EXPORT_GEOJSON.out.geojson
    merged_csv  = MERGE_QUANT_CSVS.out.merged_csv
    pyramid     = MERGE_AND_PYRAMID.out.pyramid
    qc          = ch_qc
    seg_qc      = ch_seg_qc
    versions    = ch_versions
    size_logs   = ch_size_logs
}
