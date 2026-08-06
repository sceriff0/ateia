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
include { EXTRACT_MASK_SERIES      } from '../../modules/local/extract_mask_series'
include { EXTRACT_CELL_PROPERTIES  } from '../../modules/local/extract_cell_properties'
include { EXTRACT_NUCLEI_PROPERTIES } from '../../modules/local/extract_nuclei_properties'
include { SPLIT_CHANNELS           } from '../../modules/local/split_channels'
include { SPLIT_CHANNELS as SPLIT_PRIOR_PYRAMID } from '../../modules/local/split_channels'
include { MERGE_QUANT_CSVS         } from '../../modules/local/merge_quant_csvs'
include { GENERATE_REGISTRATION_QC } from '../../modules/local/generate_registration_qc'
include { SEG_QC                   } from './seg_qc'
// Shared with subworkflows/local/postprocess.nf. These used to be copied inline here;
// the copy had already drifted (bare .groupTuple() instead of the sized groupKey).
include { QUANTIFY_MARKERS         } from './quantify_markers'
include { ASSEMBLE_EXPORT          } from './assemble_export'

workflow ADD_CYCLE {
    take:
    ch_new_input     // [meta, raw_file]  (new cycle slides, is_reference=false)

    main:
    // ------------------------------------------------------------------ //
    // 0. PRIOR ASSETS — everything this subworkflow reuses from the prior
    //    completed run, rebuilt from that run's checkpoint CSVs.
    // ------------------------------------------------------------------ //
    // This used to be assembled by the caller (workflows/mirage.nf) and handed
    // over as a bare 7-tuple, which this file then destructured positionally
    // eleven times with `_`-prefixed throwaways. It lives here now, and the
    // per-patient payload is a NAMED map:
    //
    //   [patient_id, [ ref_channels : List<String>  channels of the frozen reference
    //                , ref_image    : path          the frozen registration target
    //                , base_csv     : path          prior merged quantification table
    //                , cell_mask    : path          prior segmentation cell mask
    //                , nuclei_mask  : path          prior segmentation nuclei mask
    //                , pyramid      : path          prior combined pyramid ]]
    //
    // patient_id stays a bare first element so the joins/combines below can key
    // on it with `by: 0`.
    //
    // The prerequisites (--prior_outdir exists, every new-cycle patient appears
    // in the prior postprocessed.csv, --mode add_cycle) are validated in
    // workflows/mirage.nf before this subworkflow is ever invoked.

    // registered.csv: patient_id,registered_image,is_reference,channels
    ch_prior_ref = Channel
        .fromPath(Layout.checkpointCsv(params.prior_outdir, Layout.REGISTERED), checkIfExists: true)
        .splitCsv(header: true)
        .filter { row -> row.is_reference?.toLowerCase() == 'true' }
        .map { row ->
            def chans = (row.channels ?: '').split('\\|').collect { it.trim() }.findAll { it }
            [row.patient_id, chans, file(row.registered_image)]
        }

    // postprocessed.csv: patient_id,cell_csv,cell_geojson,merged_csv,cell_mask,pyramid
    // The cell_mask column is parsed (so a checkpoint missing it still fails loudly,
    // as before) but deliberately NOT used: the reusable masks come from the prior
    // pyramid's embedded mask series, which is the copy that is registered to the
    // pyramid's own frame.
    ch_prior_rows = Channel
        .fromPath(Layout.checkpointCsv(params.prior_outdir, Layout.POSTPROCESSED), checkIfExists: true)
        .splitCsv(header: true)
        .map { row -> [row.patient_id, file(row.merged_csv), file(row.cell_mask), file(row.pyramid)] }

    // Extract masks from the prior pyramid's Image:1 series (fast-fails in the
    // process if the prior run was not embed_masks+expanded+compartment).
    EXTRACT_MASK_SERIES(ch_prior_rows.map { pid, _merged_csv, _cell_mask, pyramid -> [[patient_id: pid, id: pid], pyramid] })
    ch_extracted_masks = EXTRACT_MASK_SERIES.out.cell_mask.map { m, f -> [m.patient_id, f] }
        .join(EXTRACT_MASK_SERIES.out.nuclei_mask.map { m, f -> [m.patient_id, f] }, by: 0)

    ch_prior_assets = ch_prior_ref
        .join(ch_prior_rows.map { pid, merged_csv, _cell_mask, pyramid -> [pid, merged_csv, pyramid] }, by: 0)
        .join(ch_extracted_masks, by: 0)
        .map { pid, ref_channels, ref_image, base_csv, pyramid, cell_mask, nuclei_mask ->
            [pid, [
                ref_channels: ref_channels,
                ref_image   : ref_image,
                base_csv    : base_csv,
                cell_mask   : cell_mask,
                nuclei_mask : nuclei_mask,
                pyramid     : pyramid,
            ]]
        }

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
    ch_ref_item = ch_prior_assets.map { pid, prior ->
        def ref_meta = [patient_id: pid, id: "${pid}_reference", is_reference: true, channels: prior.ref_channels]
        [pid, [ref_meta, prior.ref_image]]
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
            .combine(ch_prior_assets.map { pid, prior -> [pid, prior.ref_image] }, by: 0)
            .map { _pid, meta, reg_f, ref_f -> [meta, reg_f, ref_f] }
        GENERATE_REGISTRATION_QC(ch_for_qc)
        ch_qc = GENERATE_REGISTRATION_QC.out.qc
    }

    // Level >= 2: seg-overlap Dice/IoU. Segment DAPI on the NATIVE (pre-reg)
    // reference + new-cycle images -> cell GeoJSON, then warp through the
    // classic registrar pickle (classic adapter only — see do_seg_qc above).
    // Shares subworkflows/local/seg_qc.nf with registration.nf. add_cycle is VALIS-only
    // (mirage.nf rejects --registration_method tiled in this mode), so the two tiled-only
    // inputs are empty and SEG_QC always takes its valis branch.
    ch_seg_qc_size_log = Channel.empty()
    ch_seg_qc_versions = Channel.empty()
    if (do_seg_qc) {
        ch_native = ch_new_pre
            .map { meta, f -> [meta + [is_reference: false], f] }
            .mix(ch_prior_assets.map { pid, prior ->
                [[patient_id: pid, id: "${pid}_reference", is_reference: true, channels: prior.ref_channels], prior.ref_image]
            })

        SEG_QC(ch_native, ch_registrar, VALIS_ADAPTER.out.stage_checkpoint, Channel.empty())
        ch_seg_qc          = SEG_QC.out.metrics
        ch_seg_qc_size_log = SEG_QC.out.size_log
        ch_seg_qc_versions = SEG_QC.out.versions
    }

    // ------------------------------------------------------------------ //
    // 4. REUSE prior masks -> cell contours (+ nucleus contours if compartments)
    //    Masks are unchanged, so cell labels are identical.
    // ------------------------------------------------------------------ //
    ch_prior_cell_mask = ch_prior_assets.map { pid, prior ->
        [[patient_id: pid, id: pid, is_reference: true], prior.cell_mask]
    }
    EXTRACT_CELL_PROPERTIES(ch_prior_cell_mask)
    ch_contours = EXTRACT_CELL_PROPERTIES.out.contours.map { meta, j -> [meta.patient_id, j] }

    // Nucleus contours (re-keyed to cell labels) — only when compartments enabled.
    ch_nucleus_contours = Channel.empty()
    if (params.quantify_compartments) {
        ch_nuclei_props_in = ch_prior_assets.map { pid, prior ->
            [[patient_id: pid, id: pid], prior.nuclei_mask, prior.cell_mask]
        }
        EXTRACT_NUCLEI_PROPERTIES(ch_nuclei_props_in)
        ch_nucleus_contours = EXTRACT_NUCLEI_PROPERTIES.out.contours.map { meta, j -> [meta.patient_id, j] }
    }

    // ------------------------------------------------------------------ //
    // 5. SPLIT new registered cycle -> per-marker single-channel TIFFs
    //    (SPLIT_CHANNELS drops the new cycle's DAPI: non-reference input.)
    // ------------------------------------------------------------------ //
    SPLIT_CHANNELS(ch_new_registered.map { meta, f -> [meta, f, false] })

    // ------------------------------------------------------------------ //
    // 6. QUANTIFY new markers against the REUSED masks. QUANTIFY reads
    //    params.quantify_compartments to route the nuclei mask; --expanded
    //    arrives via ext.args (conf/modules.config), so no change here.
    //
    //    The per-marker fan-out, the mask combine and the per-patient grouping
    //    are QUANTIFY_MARKERS, shared with postprocess.nf — including the
    //    groupKey(patient_id, channels_count) streaming hint that this file's
    //    former inline copy had lost.
    // ------------------------------------------------------------------ //
    ch_masks = ch_prior_assets.map { pid, prior ->
        [pid, prior.cell_mask, prior.nuclei_mask]
    }
    QUANTIFY_MARKERS(SPLIT_CHANNELS.out.channels, ch_masks)

    // ------------------------------------------------------------------ //
    // 7. MERGE new marker CSVs onto the prior merged table (base) by label
    // ------------------------------------------------------------------ //
    // QUANTIFY_MARKERS emits [meta, csvs]; add_cycle keys the merge by patient
    // and builds its own patient-level meta (no morphology join here — the third
    // MERGE_QUANT_CSVS slot carries the prior base table instead).
    ch_new_quant_grouped = QUANTIFY_MARKERS.out.grouped_csv
        .map { meta, csvs -> [meta.patient_id, csvs] }
    ch_base = ch_prior_assets.map { pid, prior -> [pid, prior.base_csv] }
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
    // No PHENOTYPE stage here: both phenotype slots reuse the cell contours file.
    ch_pheno_extras = ch_contours.map { pid, contours -> [pid, contours, contours] }

    // ------------------------------------------------------------------ //
    // 9. REBUILD complete pyramid: recover prior channels from the prior
    //    pyramid (is_reference=true keeps ref DAPI + all old markers), then
    //    merge with the new cycle's channels.
    // ------------------------------------------------------------------ //
    ch_prior_pyramid = ch_prior_assets.map { pid, prior ->
        [[patient_id: pid, id: pid, is_reference: true, channels: []], prior.pyramid, true]
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

    // ------------------------------------------------------------------ //
    // 10. EXPORT + PYRAMID (ASSEMBLE_EXPORT, shared with postprocess.nf)
    // ------------------------------------------------------------------ //
    // ASSEMBLE_EXPORT owns the EXPORT_GEOJSON tuple and the embed_masks gate.
    // ADD_CYCLE feeds it the SAME cell + nuclei masks it reused from
    // ch_prior_assets (they are unchanged here — no re-derivation), so the
    // rebuilt combined pyramid carries the mask series whenever embed_masks is on.
    ASSEMBLE_EXPORT(
        MERGE_QUANT_CSVS.out.merged_csv,
        ch_contours,
        ch_nuc_for_export,
        ch_pheno_extras,
        ch_all_channels,
        ch_masks
    )

    // ------------------------------------------------------------------ //
    // Versions + size logs
    // ------------------------------------------------------------------ //
    // QUANTIFY_MARKERS / ASSEMBLE_EXPORT already applied `.first()` internally
    // (see the comments on their `versions` emits) — do not re-apply it here.
    //
    // EXTRACT_MASK_SERIES.out.versions is deliberately NOT mixed in: it was not
    // collected when the process was invoked from workflows/mirage.nf either, and
    // adding it here would change collated_versions.yml (and therefore the QC
    // report's version table) as a side effect of moving the call. Wiring it up is
    // a real gap, but a behaviour change that belongs in its own commit.
    ch_versions = Channel.empty()
        .mix(PREPROCESSING.out.versions)
        .mix(ch_adapter_versions)
        .mix(EXTRACT_CELL_PROPERTIES.out.versions.first())
        .mix(SPLIT_CHANNELS.out.versions.first())
        .mix(SPLIT_PRIOR_PYRAMID.out.versions.first())
        .mix(QUANTIFY_MARKERS.out.versions)
        .mix(MERGE_QUANT_CSVS.out.versions.first())
        .mix(ASSEMBLE_EXPORT.out.versions)

    ch_size_logs = Channel.empty()
        .mix(SPLIT_CHANNELS.out.size_log)
        .mix(SPLIT_PRIOR_PYRAMID.out.size_log)
        .mix(QUANTIFY_MARKERS.out.size_logs)
        .mix(MERGE_QUANT_CSVS.out.size_log)
        .mix(ASSEMBLE_EXPORT.out.size_logs)
        .mix(EXTRACT_CELL_PROPERTIES.out.size_log)

    if (reg_qc_level >= 1) {
        ch_versions  = ch_versions.mix(GENERATE_REGISTRATION_QC.out.versions.first())
        ch_size_logs = ch_size_logs.mix(GENERATE_REGISTRATION_QC.out.size_log)
    }
    // SEG_QC's emissions already cover SEG_QC_GEOJSON + WARP_SEG_QC (versions pre-.first()).
    if (do_seg_qc) {
        ch_versions  = ch_versions.mix(ch_seg_qc_versions)
        ch_size_logs = ch_size_logs.mix(ch_seg_qc_size_log)
    }
    if (params.quantify_compartments) {
        ch_versions  = ch_versions.mix(EXTRACT_NUCLEI_PROPERTIES.out.versions.first())
        ch_size_logs = ch_size_logs.mix(EXTRACT_NUCLEI_PROPERTIES.out.size_log)
    }

    emit:
    geojson     = ASSEMBLE_EXPORT.out.geojson
    merged_csv  = MERGE_QUANT_CSVS.out.merged_csv
    pyramid     = ASSEMBLE_EXPORT.out.pyramid
    qc          = ch_qc
    seg_qc      = ch_seg_qc
    versions    = ch_versions
    size_logs   = ch_size_logs
}
