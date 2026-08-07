
/*
========================================================================================
    IMPORT MODULES
========================================================================================
*/

include { SEGMENT                  } from '../../modules/local/segment'
include { EXTRACT_CELL_PROPERTIES } from '../../modules/local/extract_cell_properties'
include { EXTRACT_NUCLEI_PROPERTIES } from '../../modules/local/extract_nuclei_properties'
include { SPLIT_CHANNELS           } from '../../modules/local/split_channels'
include { MERGE_QUANT_CSVS         } from '../../modules/local/merge_quant_csvs'
include { COMPILE_PANEL            } from '../../modules/local/compile_panel'
include { PHENOTYPE                } from '../../modules/local/phenotype'
include { GENERATE_POSTPROCESSING_QC    } from '../../modules/local/generate_postprocessing_qc'
include { EXPORT_SPATIALDATA } from '../../modules/local/export_spatialdata'
// Shared with subworkflows/local/add_cycle.nf — see those files for why the shaping
// lives there rather than being copied into each caller.
include { QUANTIFY_MARKERS         } from './quantify_markers'
include { ASSEMBLE_EXPORT          } from './assemble_export'

/*
========================================================================================
    SUBWORKFLOW:POSTPROCESSING
========================================================================================
    Description:
        Segments reference image, splits multichannel images to single channels,
        quantifies marker intensities per cell, merges results, and exports
        QuPath-compatible GeoJSON with raw measurements for FlowPath gating.

    Input:
        ch_registered: Channel of [meta, file] tuples for registered images

    Output:
        geojson: QuPath-compatible GeoJSON with cell detections and raw measurements
        cell_csv: Cell data CSV with raw intensities and z-scores
        merged_csv: Merged quantification CSV
        cell_mask: Cell segmentation mask
========================================================================================
*/

workflow POSTPROCESSING {
    take:
    ch_registered       // Channel of [meta, file] tuples
    ch_reg_qc           // Registration QC JSONs (may be empty)
    ch_reg_residuals    // Per-cell registration residual CSVs (may be empty)

    main:

    // ========================================================================
    // SEGMENTATION - Process reference images only
    // ========================================================================
    ch_references = ch_registered
        .filter { meta, file -> meta.is_reference }

    ch_references.ifEmpty {
        error "No reference images found (is_reference=true). Cannot run segmentation."
    }

    SEGMENT(ch_references)

    def ch_cell_mask   = SEGMENT.out.cell_mask
    def ch_nuclei_mask = SEGMENT.out.nuclei_mask

    // ========================================================================
    // CELL PROPERTIES - Extract morphology + contours from mask (runs in PARALLEL with SPLIT_CHANNELS)
    // Computes regionprops ONCE instead of N times in QUANTIFY
    // ========================================================================
    EXTRACT_CELL_PROPERTIES(ch_cell_mask)

    // Nucleus contours (re-keyed to cell labels) for dual-segmentation GeoJSON export.
    // Only computed when per-compartment quantification is enabled.
    // Default to empty so the channel is always defined (the export join below
    // only consumes it when quantify_compartments is set, but an unassigned
    // `def` is a fragile null to leave in a channel expression).
    def ch_nucleus_contours = Channel.empty()
    if (params.quantify_compartments) {
        ch_nuclei_props_in = ch_nuclei_mask
            .map { meta, mask -> [meta.patient_id, meta, mask] }
            .join(ch_cell_mask.map { meta, mask -> [meta.patient_id, mask] }, by: 0)
            .map { _patient_id, meta, nuclei_mask, cell_mask -> [meta, nuclei_mask, cell_mask] }
        EXTRACT_NUCLEI_PROPERTIES(ch_nuclei_props_in)
        ch_nucleus_contours = EXTRACT_NUCLEI_PROPERTIES.out.contours
            .map { meta, json_file -> [meta.patient_id, json_file] }
    }

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
    QUANTIFY_MARKERS(SPLIT_CHANNELS.out.channels, ch_mask)
    ch_grouped_csvs = QUANTIFY_MARKERS.out.grouped_csv

    // Join grouped intensity CSVs with morphology.csv from EXTRACT_CELL_PROPERTIES
    ch_morphology = EXTRACT_CELL_PROPERTIES.out.morphology
        .map { meta, csv -> [meta.patient_id, csv] }

    ch_for_quant_merge = ch_grouped_csvs
        .map { meta, csvs -> [meta.patient_id, meta, csvs] }
        .join(ch_morphology, by: 0)
        .map { _patient_id, meta, csvs, morphology_csv -> [meta, csvs, morphology_csv] }

    MERGE_QUANT_CSVS(ch_for_quant_merge)

    // ========================================================================
    // PHENOTYPING (optional) - compile panel + classify cells per patient
    // ========================================================================
    ch_contours = EXTRACT_CELL_PROPERTIES.out.contours
        .map { meta, json_file -> [meta.patient_id, json_file] }
    ch_nuc_contours_for_export = params.quantify_compartments ? ch_nucleus_contours : ch_contours

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
            .join(ch_morphology, by: 0)
            .map { _pid, meta, csv, morph -> [meta, csv, morph] }
        PHENOTYPE(ch_pheno_in, ch_model_config)
        ch_phenotypes = PHENOTYPE.out.phenotypes
    }

    // Phenotype/model-config slots for EXPORT_GEOJSON, keyed by patient_id.
    // With a panel: the real PHENOTYPE output plus the compiled model config.
    // Without: reuse the contours file as harmless placeholders; the module guard
    // (params.panel_spec || params.panel_model) suppresses the args.
    def ch_pheno_extras = do_pheno
        ? ch_phenotypes.map { meta, ph -> [meta.patient_id, ph] }.combine(ch_model_config)
        : ch_contours.map { pid, contours_json -> [pid, contours_json, contours_json] }

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
    // channels_count semantics even though their downstream failure modes differ.
    ch_split_grouped = SPLIT_CHANNELS.out.channels
        .flatMap { meta, tiffs ->
            // Normalize to List and create entries keyed by [patient_id, marker]
            // Carry channels_count for groupKey
            def tiff_list = tiffs instanceof List ? tiffs : [tiffs]
            tiff_list.collect { tiff ->
                [meta.patient_id, meta.channels_count, tiff.baseName, tiff]
            }
        }
        .unique { patient_id, _channels_count, marker, _tiff -> [patient_id, marker] }  // Keep first occurrence of each patient+marker
        .map { patient_id, channels_count, _marker, tiff ->
            // Use groupKey for streaming if channels_count is available
            def gkey = channels_count
                ? groupKey(patient_id, channels_count)
                : patient_id
            [gkey, tiff]
        }
        .groupTuple(by: 0, remainder: true)
        .map { patient_id, tiffs ->
            // Create patient-level metadata
            // Extract actual patient_id from groupKey wrapper
            def pid = patient_id.toString()
            def patient_meta = [
                id: pid,
                patient_id: pid,
                is_reference: false  // Not relevant at patient level
            ]
            [patient_meta, tiffs]
        }

    // EXPORT_GEOJSON tuple assembly + the embed_masks pyramid gate live in
    // ASSEMBLE_EXPORT, shared with add_cycle.nf.
    ASSEMBLE_EXPORT(
        MERGE_QUANT_CSVS.out.merged_csv,
        ch_contours,
        ch_nuc_contours_for_export,
        ch_pheno_extras,
        ch_split_grouped,
        ch_mask
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
            def published_path = Layout.publishedPath(params.outdir, meta.patient_id, 'segmentation', mask)
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
        def ch_sd_in = MERGE_QUANT_CSVS.out.merged_csv
            .map { meta, csv -> [meta.patient_id, meta, csv] }
            .join(ch_contours, by: 0)
            .join(ch_nuc_contours_for_export, by: 0)
            .join(ch_cell_mask.map { meta, m -> [meta.patient_id, m] }, by: 0)
            .join(ch_nuclei_mask.map { meta, m -> [meta.patient_id, m] }, by: 0)
            .join(ASSEMBLE_EXPORT.out.pyramid.map { meta, p -> [meta.patient_id, p] }, by: 0)
            .map { _pid, meta, csv, contours, nuc_contours, cmask, nmask, pyramid ->
                [meta, csv, contours, nuc_contours, cmask, nmask, pyramid]
            }

        // QC is collected run-wide rather than per patient: these channels are already
        // flat file streams by the time they arrive, and `.ifEmpty([])` keeps the export
        // running when reg_qc=0 or the run started at postprocessing.
        EXPORT_SPATIALDATA(
            ch_sd_in,
            ch_reg_qc.map { it instanceof List ? it[-1] : it }.flatten().collect().ifEmpty([]),
            ch_reg_residuals.map { it instanceof List ? it[-1] : it }.flatten().collect().ifEmpty([])
        )
    }

    ch_checkpoint_csv = ch_base_checkpoint
        .map { patient_id, cell_csv, cell_geojson, merged_csv, cell_mask, pyramid ->
            Checkpoint.row(Layout.POSTPROCESSED, [
                patient_id  : patient_id,
                cell_csv    : cell_csv,
                cell_geojson: cell_geojson,
                merged_csv  : merged_csv,
                cell_mask   : cell_mask,
                pyramid     : pyramid,
            ])
        }
        .collectFile(
            name: Layout.checkpointCsvName(Layout.POSTPROCESSED),
            newLine: true,
            sort: true,
            // sort: true makes the manifest REPRODUCIBLE. Without it collectFile
            // writes rows in completion order, so two runs of the same commit
            // produced different files (found while capturing this branch's golden
            // baseline; a rerun of the UNMODIFIED branch differed from itself). The
            // rows begin with patient_id followed by the published path, so natural
            // string order IS "patient id, then file" — and the `seed:` header is
            // written first regardless of sorting.
            storeDir: Layout.checkpointDir(params.outdir),
            seed: Checkpoint.header(Layout.POSTPROCESSED)
        )

    // Collect size logs from all postprocessing processes
    ch_size_logs = Channel.empty()
        .mix(SEGMENT.out.size_log)
        .mix(EXTRACT_CELL_PROPERTIES.out.size_log)
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

    // Fold in nucleus-properties traces/versions when compartment quantification ran.
    if (params.quantify_compartments) {
        ch_size_logs = ch_size_logs.mix(EXTRACT_NUCLEI_PROPERTIES.out.size_log)
    }

    // Collect versions from all postprocessing processes.
    // QUANTIFY_MARKERS / ASSEMBLE_EXPORT already applied `.first()` internally
    // (see the comments on their `versions` emits) — do not re-apply it here.
    ch_versions = Channel.empty()
        .mix(SEGMENT.out.versions.first())
        .mix(EXTRACT_CELL_PROPERTIES.out.versions.first())
        .mix(SPLIT_CHANNELS.out.versions.first())
        .mix(QUANTIFY_MARKERS.out.versions)
        .mix(MERGE_QUANT_CSVS.out.versions.first())
        .mix(ASSEMBLE_EXPORT.out.versions)

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

    if (params.quantify_compartments) {
        ch_versions = ch_versions
            .mix(EXTRACT_NUCLEI_PROPERTIES.out.versions.first())
    }

    emit:
    checkpoint_csv    = ch_checkpoint_csv
    postprocess_qc    = ch_postprocess_qc
    size_logs         = ch_size_logs
    versions          = ch_versions
}
