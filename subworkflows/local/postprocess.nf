
/*
========================================================================================
    IMPORT MODULES
========================================================================================
*/

include { SPLIT_CHANNELS           } from '../../modules/local/split_channels'
include { MERGE_QUANT_CSVS         } from '../../modules/local/merge_quant_csvs'
include { COMPILE_PANEL            } from '../../modules/local/compile_panel'
include { PHENOTYPE                } from '../../modules/local/phenotype'
include { GENERATE_POSTPROCESSING_QC    } from '../../modules/local/generate_postprocessing_qc'
include { EXPORT_SPATIALDATA } from '../../modules/local/export_spatialdata'
// Shared with subworkflows/local/add_cycle.nf — see those files for why the shaping
// lives there rather than being copied into each caller. groupTiffsByPatient is a
// plain function, not a process/workflow, but Nextflow's `include` pulls in either.
include { QUANTIFY_MARKERS; groupTiffsByPatient } from './quantify_markers'
include { ASSEMBLE_EXPORT          } from './assemble_export'
include { CHECKPOINT_WRITER        } from './checkpoint_writer'

/*
========================================================================================
    SUBWORKFLOW:POSTPROCESSING
========================================================================================
    Description:
        Splits multichannel images to single channels, quantifies marker intensities
        per cell against masks produced upstream by subworkflows/local/segmentation.nf,
        merges results, and exports QuPath-compatible GeoJSON with raw measurements
        for FlowPath gating.

        SEGMENT / EXTRACT_CELL_PROPERTIES / EXTRACT_NUCLEI_PROPERTIES no longer run
        here — they moved to segmentation.nf, their own resumable step
        (Layout.SEGMENTED / Checkpoint.columns('segmented')). This file takes their
        outputs as plain [meta/patient_id, file] inputs instead.

    Input:
        ch_registered:       [meta, file] — all registered slides (reference + moving)
        ch_cell_mask:        [meta, file] — from segmentation.nf
        ch_nuclei_mask:      [meta, file] — from segmentation.nf
        ch_contours:         [patient_id, file] — from segmentation.nf
        ch_nucleus_contours: [patient_id, file] — from segmentation.nf;
                              Channel.empty() when --quantify_compartments is false
        ch_morphology:       [meta, file] — from segmentation.nf

    Output:
        checkpoint_csv: file — the collected 'postprocessed' checkpoint (see
                        Layout.POSTPROCESSED / Checkpoint.columns), one row per patient
        postprocess_qc: GENERATE_POSTPROCESSING_QC's per-patient QC artifacts
        size_logs:      input size-log CSVs from this step's processes
        versions:       versions.yml from this step's processes
========================================================================================
*/

workflow POSTPROCESSING {
    take:
    ch_registered       // Channel of [meta, file] tuples
    ch_cell_mask        // [meta, file] — subworkflows/local/segmentation.nf's SEGMENT.out.cell_mask
    ch_nuclei_mask      // [meta, file] — segmentation.nf's SEGMENT.out.nuclei_mask
    ch_contours         // [patient_id, file] — segmentation.nf's EXTRACT_CELL_PROPERTIES.out.contours
    ch_nucleus_contours // [patient_id, file] — segmentation.nf's EXTRACT_NUCLEI_PROPERTIES.out.contours;
                        // Channel.empty() when !params.quantify_compartments
    ch_morphology       // [meta, file] — segmentation.nf's EXTRACT_CELL_PROPERTIES.out.morphology
    ch_reg_qc           // Registration QC JSONs (may be empty)
    ch_reg_residuals    // Per-cell registration residual CSVs (may be empty)
    compartment_mode    // ParamUtils.compartmentMode(params) — resolved once by
                        // workflows/mirage.nf and threaded down, the same seam
                        // --registration_method has. Passed straight through to
                        // ASSEMBLE_EXPORT below; `.compartments` is also read here
                        // directly for the nucleus-contour ternary.

    main:

    // ========================================================================
    // CHANNEL SPLITTING - Split all multichannel images (runs in PARALLEL with EXTRACT_CELL_PROPERTIES)
    // ========================================================================
    SPLIT_CHANNELS(
        ch_registered.map { meta, file -> [meta, file, meta.is_reference] }
    )

    // ========================================================================
    // QUANTIFICATION - Join channels with their patient's mask
    // ========================================================================
    // Carry BOTH masks (cell + nuclei) keyed by patient_id. The nuclear mask is
    // always available from SEGMENT; QUANTIFY only uses it when
    // params.quantify_compartments is set (per-compartment signal). The same pair
    // feeds ASSEMBLE_EXPORT's embed_masks gate further down.
    ch_mask = ch_cell_mask
        .map { meta, mask -> [meta.patient_id, mask] }
        .join(ch_nuclei_mask.map { meta, mask -> [meta.patient_id, mask] }, by: 0)

    // Per-marker fan-out + QUANTIFY + per-patient grouping (with the groupKey
    // streaming hint) all live in QUANTIFY_MARKERS, shared with add_cycle.nf.
    // The --debug_channels views for this whole chain moved there with it, so this
    // file no longer carries a debug-view helper of its own.
    QUANTIFY_MARKERS(SPLIT_CHANNELS.out.channels, ch_mask, compartment_mode)
    ch_grouped_csvs = QUANTIFY_MARKERS.out.grouped_csv

    // Join grouped intensity CSVs with morphology.csv (segmentation.nf's
    // EXTRACT_CELL_PROPERTIES.out.morphology, taken in via ch_morphology)
    ch_morphology_by_patient = ch_morphology
        .map { meta, csv -> [meta.patient_id, csv] }

    ch_for_quant_merge = ch_grouped_csvs
        .map { meta, csvs -> [meta.patient_id, meta, csvs] }
        .join(ch_morphology_by_patient, by: 0)
        .map { _patient_id, meta, csvs, morphology_csv -> [meta, csvs, morphology_csv] }

    MERGE_QUANT_CSVS(ch_for_quant_merge)

    // ========================================================================
    // PHENOTYPING (optional) - compile panel + classify cells per patient
    // ========================================================================
    // ch_contours arrives via take: (segmentation.nf's EXTRACT_CELL_PROPERTIES.out.contours,
    // already re-keyed to patient_id) — nothing to derive here any more.
    ch_nuc_contours_for_export = compartment_mode.compartments ? ch_nucleus_contours : ch_contours

    def do_pheno = (params.panel_spec != null) || (params.panel_model != null)
    def ch_model_config = Channel.empty()
    def ch_phenotypes = Channel.empty()
    if (do_pheno) {
        if (params.panel_spec) {
            COMPILE_PANEL(Channel.value(file(params.panel_spec)))
            ch_model_config = COMPILE_PANEL.out.model_config.first()
        } else {
            ch_model_config = Channel.value(file(params.panel_model))
        }

        ch_pheno_in = MERGE_QUANT_CSVS.out.merged_csv
            .map { meta, csv -> [meta.patient_id, meta, csv] }
            .join(ch_morphology_by_patient, by: 0)
            .map { _pid, meta, csv, morph -> [meta, csv, morph] }
        PHENOTYPE(ch_pheno_in, ch_model_config)
        ch_phenotypes = PHENOTYPE.out.phenotypes
    }

    // Phenotype/model-config/QC slots for EXPORT_GEOJSON, keyed by patient_id.
    // With a panel: PHENOTYPE's phenotypes.csv and phenotype_qc.json (joined on
    // patient_id, since they are separate emits of the same task) plus the run-level
    // compiled model config.
    // Without: reuse the contours file as harmless placeholders; the module guard
    // (params.panel_spec || params.panel_model) suppresses the args. The placeholder
    // arity must match the real one or the join downstream silently mismatches.
    def ch_pheno_extras = do_pheno
        ? ch_phenotypes.map { meta, ph -> [meta.patient_id, ph] }
            .join(PHENOTYPE.out.qc.map { meta, qc -> [meta.patient_id, qc] })
            .combine(ch_model_config)
            .map { pid, ph, qc, cfg -> [pid, ph, cfg, qc] }
        : ch_contours.map { pid, contours_json -> [pid, contours_json, contours_json, contours_json] }

    // ========================================================================
    // MERGE - Combine split channel TIFFs with segmentation mask (per patient)
    // ========================================================================
    // Group split channel TIFFs by patient for merging
    // SPLIT_CHANNELS already handles DAPI filtering correctly
    // Deduplicate by patient_id + marker to avoid duplicate channel names
    // Use groupKey for streaming - emits as soon as channels_count items collected
    //
    // `remainder: true` for the same reason as QUANTIFY_MARKERS' grouping (same
    // channels_count): an under-count must not be allowed to silently drop the patient
    // from the pyramid outright. But — see the fuller account in QUANTIFY_MARKERS'
    // GROUP comment, which this grouping mirrors — an under-count here does NOT degrade
    // to "late but complete" the way it can for the CSV-merge paths. This grouping feeds
    // MERGE_AND_PYRAMID with a one-file surplus group, which trips that process's memory
    // closure (conf/modules.config:330-337 — pre-existing, NOT fixed here) and the run
    // ABORTS with "No such file or directory: channels". `remainder: true` is kept anyway
    // because keeping only one of the two channels_count-sized groupings (this one, or
    // QUANTIFY_MARKERS') would be worse than keeping neither — the patient would reach
    // geojson/ and quantification/ but not the pyramid, and so be missing from
    // csv/postprocessed.csv, half-published and invisible to any later --start
    // postprocessing or add_cycle run. These two groupings must keep identical
    // channels_count semantics even though their downstream failure modes differ --
    // WITHIN THIS FILE: both read the same meta.channels_count for the same
    // patient. That equality is NOT a cross-file invariant: add_cycle.nf's own
    // pyramid grouping deliberately uses a DIFFERENT total (new_count + prior_count,
    // since it merges two pyramids' worth of channels) than its own QUANTIFY_MARKERS
    // call (new-cycle channels only, because the prior quantification columns are
    // merged onto the CSV separately, not re-quantified) -- the two counts SHOULD
    // differ there. Do not "reconcile" them to match this file's equality.
    // The channels_count-sized groupKey + remainder:true grouping itself is
    // groupTiffsByPatient (subworkflows/local/quantify_markers.nf), shared with
    // add_cycle.nf's own version of this same grouping — see that function's doc
    // comment for why an under-count here is worse than for the CSV-merge paths.
    ch_split_tagged = SPLIT_CHANNELS.out.channels
        .flatMap { meta, tiffs ->
            // Normalize to List and create entries keyed by [patient_id, marker]
            // Carry channels_count for groupKey
            def tiff_list = tiffs instanceof List ? tiffs : [tiffs]
            tiff_list.collect { tiff ->
                [meta.patient_id, meta.channels_count, tiff.baseName, tiff]
            }
        }
        .unique { patient_id, _channels_count, marker, _tiff -> [patient_id, marker] }  // Keep first occurrence of each patient+marker
        .map { patient_id, channels_count, _marker, tiff -> [patient_id, channels_count, tiff] }
    ch_split_grouped = groupTiffsByPatient(ch_split_tagged)

    // EXPORT_GEOJSON tuple assembly + the embed_masks pyramid gate live in
    // ASSEMBLE_EXPORT, shared with add_cycle.nf.
    ASSEMBLE_EXPORT(
        MERGE_QUANT_CSVS.out.merged_csv,
        ch_contours,
        ch_nuc_contours_for_export,
        ch_pheno_extras,
        ch_split_grouped,
        ch_mask,
        compartment_mode
    )

    // ========================================================================
    // POSTPROCESSING QC (optional, runs in PARALLEL with MERGE_AND_PYRAMID)
    // ========================================================================
    ch_postprocess_qc = Channel.empty()
    if (!params.skip_postprocessing_qc) {
        // Join cell mask with merged CSV for QC visualization
        ch_for_postprocess_qc = ch_cell_mask
            .map { meta, mask -> [meta.patient_id, meta, mask] }
            .join(
                MERGE_QUANT_CSVS.out.merged_csv.map { meta, csv -> [meta.patient_id, csv] },
                by: 0
            )
            .map { _patient_id, meta, mask, csv -> [meta, mask, csv] }

        GENERATE_POSTPROCESSING_QC(ch_for_postprocess_qc)
        ch_postprocess_qc = GENERATE_POSTPROCESSING_QC.out.qc.map { meta, pngs -> pngs }
    }

    // ========================================================================
    // CHECKPOINT - Collect all outputs by patient
    // ========================================================================
    // Use collectFile() for non-blocking aggregation (enables patient-level parallelism)
    // The join chain is kept (it's per-patient and doesn't block other patients)

    // Base checkpoint data (always present)
    ch_base_checkpoint = ASSEMBLE_EXPORT.out.csv
        .map { meta, csv ->
            def published_path = Layout.publishedPath(params.outdir, meta.patient_id, 'geojson', csv)
            [meta.patient_id, published_path]
        }
        .join(ASSEMBLE_EXPORT.out.geojson.map { meta, geojson ->
            def published_path = Layout.publishedPath(params.outdir, meta.patient_id, 'geojson', geojson)
            [meta.patient_id, published_path]
        })
        .join(MERGE_QUANT_CSVS.out.merged_csv.map { meta, csv ->
            def published_path = Layout.publishedPath(params.outdir, meta.patient_id, 'quantification', csv)
            [meta.patient_id, published_path]
        })
        .join(ch_cell_mask.map { meta, mask ->
            // publishedOrAsIs, not publishedPath: with --start postprocessing,
            // ch_cell_mask is READ_SEGMENTED_CHECKPOINT's file(row.cell_mask) --
            // ALREADY an absolute published path from segmentation.nf's run, not a
            // task-dir output of this run. Calling publishedPath unconditionally
            // would double-nest it (producerSubdir sees a parent literally named
            // 'segmentation' and treats it as a producer subdirectory to preserve),
            // e.g. <outdir>/<pid>/segmentation/segmentation/<name>. See
            // Layout.publishedOrAsIs for the full explanation.
            //
            // NEW CROSS-OUTDIR DEPENDENCY: at --start postprocessing this correctly
            // records the PRIOR run's path (wherever segmentation.nf actually
            // published it), not this run's --outdir -- unlike every other column in
            // this checkpoint, which all name files this run itself just wrote. That
            // is correct (the mask genuinely was not recomputed), but it means
            // csv/postprocessed.csv can point outside its own --outdir when written
            // this way, which no prior checkpoint in this pipeline ever did.
            def published_path = Layout.publishedOrAsIs(params.outdir, meta.patient_id, 'segmentation', mask)
            [meta.patient_id, published_path]
        })
        .join(ASSEMBLE_EXPORT.out.pyramid.map { meta, pyramid ->
            def published_path = Layout.publishedPath(params.outdir, meta.patient_id, 'pyramid', pyramid)
            [meta.patient_id, published_path]
        })

    // ========================================================================
    // SPATIALDATA EXPORT - scverse-native .zarr (additive; OME-TIFF + GeoJSON stay primary)
    // ========================================================================
    if (!params.skip_spatialdata_export) {
        // THE reg_qc=2 ARTIFACTS ARE JOINED PER PATIENT, NOT COLLECTED RUN-WIDE.
        //
        // They used to reach EXPORT_SPATIALDATA on two separate value channels built with
        // `.collect(sort: true)` over the whole run, and a value channel is broadcast to
        // every task — so each patient's .zarr received EVERY patient's *_seg_qc.json and
        // *_reg_residuals.csv. That is silent cross-patient contamination, not a tidiness
        // problem: bin/export_spatialdata.py joins residual rows onto this patient's cell
        // centroids by RAW reference-frame pixel coordinate, and every patient's reference
        // frame is its own pixel grid rooted at (0,0), so a foreign patient's rows land on
        // whatever cell sits within params.spatialdata_residual_join_max_px and become an
        // obsm column — a column whose entire purpose is to EXCLUDE cells — with a
        // plausible join_fraction. Reproduced on a two-patient stub run: both export tasks
        // staged all four QC files. It was invisible to CI because tests/testdata/
        // test_input.csv is one patient, and with one patient "the run's QC" and "this
        // patient's QC" are the same set. tests/testdata/test_input_two_patients.csv now
        // exists so a two-patient run is reachable.
        //
        // groupKey(patient_id, images_count - 1) is the streaming size hint: one QC
        // artifact per MOVING slide, and images_count counts the reference too. It only
        // makes a group close EARLY — `remainder: true` is what makes the group close at
        // all when the count is short (WARP_SEG_QC's per_cell output is `optional: true`,
        // and a group that never closes would hang the run rather than lose a file).
        // Patients with no QC at all (reg_qc < 2, a single-slide patient, or entry at
        // postprocessing) simply have no key here; the `remainder: true` on the joins
        // below keeps them in the export with an empty list.
        def ch_reg_qc_by_patient = ch_reg_qc
            .flatMap { meta, qc ->
                def key = meta.images_count ? groupKey(meta.patient_id, meta.images_count - 1) : meta.patient_id
                (qc instanceof List ? qc : [qc]).collect { f -> [key, f] }
            }
            .groupTuple(remainder: true)
            // Unwrap the groupKey immediately (tests/test_group_key_unwrapped.py), and sort
            // the group: these lists become `path` inputs, which Nextflow hashes
            // POSITIONALLY, and groupTuple emits in ARRIVAL order — the same reason the
            // replaced code used collect(sort: true) rather than a bare collect().
            .map { group_key, files -> [group_key.toString(), files.toSorted { a, b -> a.name <=> b.name }] }

        def ch_reg_residuals_by_patient = ch_reg_residuals
            .flatMap { meta, resid ->
                def key = meta.images_count ? groupKey(meta.patient_id, meta.images_count - 1) : meta.patient_id
                (resid instanceof List ? resid : [resid]).collect { f -> [key, f] }
            }
            .groupTuple(remainder: true)
            .map { group_key, files -> [group_key.toString(), files.toSorted { a, b -> a.name <=> b.name }] }

        def ch_sd_in = MERGE_QUANT_CSVS.out.merged_csv
            .map { meta, csv -> [meta.patient_id, meta, csv] }
            .join(ch_contours, by: 0)
            .join(ch_nuc_contours_for_export, by: 0)
            .join(ch_cell_mask.map { meta, m -> [meta.patient_id, m] }, by: 0)
            .join(ch_nuclei_mask.map { meta, m -> [meta.patient_id, m] }, by: 0)
            .join(ASSEMBLE_EXPORT.out.pyramid.map { meta, p -> [meta.patient_id, p] }, by: 0)
            // remainder: true — the QC is genuinely optional (reg_qc < 2, single-slide
            // patient, --start postprocessing). A plain join would DROP those patients'
            // exports entirely, which is how an optional input silently becomes required.
            .join(ch_reg_qc_by_patient, by: 0, remainder: true)
            .join(ch_reg_residuals_by_patient, by: 0, remainder: true)
            // `remainder: true` also emits keys present ONLY on the QC side, padded with
            // nulls (a patient with registration QC but no quantification — not reachable
            // today, but the operator can produce it and the map below cannot).
            .filter { it[1] != null }
            .map { _pid, meta, csv, contours, nuc_contours, cmask, nmask, pyramid, qc, resid ->
                [meta, csv, contours, nuc_contours, cmask, nmask, pyramid, qc ?: [], resid ?: []]
            }

        EXPORT_SPATIALDATA(ch_sd_in)
    }

    // The size hint is the literal 1, not a count read off a meta: ch_base_checkpoint is
    // a join chain keyed BY patient, so this checkpoint has exactly one row per patient
    // by construction. Nothing to count, and nothing that could disagree.
    ch_checkpoint_rows = ch_base_checkpoint
        .map { patient_id, cell_csv, cell_geojson, merged_csv, cell_mask, pyramid ->
            [patient_id, 1, Checkpoint.row(Layout.POSTPROCESSED, [
                patient_id  : patient_id,
                cell_csv    : cell_csv,
                cell_geojson: cell_geojson,
                merged_csv  : merged_csv,
                cell_mask   : cell_mask,
                pyramid     : pyramid,
            ])]
        }

    CHECKPOINT_WRITER(Layout.POSTPROCESSED, ch_checkpoint_rows)
    ch_checkpoint_csv = CHECKPOINT_WRITER.out.csv

    // Collect size logs from all postprocessing processes. SEGMENT /
    // EXTRACT_CELL_PROPERTIES / EXTRACT_NUCLEI_PROPERTIES moved to
    // subworkflows/local/segmentation.nf and report their own size_logs/versions
    // there now -- workflows/mirage.nf mixes SEGMENTATION.out.{size_logs,versions}
    // into the run-wide QC stream directly, the same way it already does for every
    // other step's aggregate output (this file's checkpoint_csv/postprocess_qc
    // pattern), so nothing from that step is double-counted or dropped here.
    ch_size_logs = Channel.empty()
        .mix(SPLIT_CHANNELS.out.size_log)
        .mix(QUANTIFY_MARKERS.out.size_logs)
        .mix(MERGE_QUANT_CSVS.out.size_log)
        .mix(ASSEMBLE_EXPORT.out.size_logs)

    if (do_pheno) {
        ch_size_logs = ch_size_logs.mix(PHENOTYPE.out.size_log)
    }

    // Add postprocessing QC size logs if enabled
    if (!params.skip_postprocessing_qc) {
        ch_size_logs = ch_size_logs
            .mix(GENERATE_POSTPROCESSING_QC.out.size_log)
    }

    // Collect versions from all postprocessing processes.
    // QUANTIFY_MARKERS / ASSEMBLE_EXPORT already applied `.first()` internally
    // (see the comments on their `versions` emits) — do not re-apply it here.
    ch_versions = Channel.empty()
        .mix(SPLIT_CHANNELS.out.versions.first())
        .mix(QUANTIFY_MARKERS.out.versions)
        .mix(MERGE_QUANT_CSVS.out.versions.first())
        .mix(ASSEMBLE_EXPORT.out.versions)
        .mix(CHECKPOINT_WRITER.out.versions.first())

    if (do_pheno) {
        ch_versions = ch_versions.mix(PHENOTYPE.out.versions.first())
        if (params.panel_spec) {
            ch_versions = ch_versions.mix(COMPILE_PANEL.out.versions.first())
        }
    }

    if (!params.skip_postprocessing_qc) {
        ch_versions = ch_versions
            .mix(GENERATE_POSTPROCESSING_QC.out.versions.first())
    }

    emit:
    checkpoint_csv    = ch_checkpoint_csv
    postprocess_qc    = ch_postprocess_qc
    size_logs         = ch_size_logs
    versions          = ch_versions
}
