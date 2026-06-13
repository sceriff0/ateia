
/*
========================================================================================
    IMPORT MODULES
========================================================================================
*/

include { SEGMENT                  } from '../../modules/local/segment'
include { EXTRACT_CELL_PROPERTIES } from '../../modules/local/extract_cell_properties'
include { EXTRACT_NUCLEI_PROPERTIES } from '../../modules/local/extract_nuclei_properties'
include { SPLIT_CHANNELS           } from '../../modules/local/split_channels'
include { QUANTIFY                 } from '../../modules/local/quantify'
include { MERGE_QUANT_CSVS         } from '../../modules/local/quantify'
include { EXPORT_GEOJSON            } from '../../modules/local/export_geojson'
include { MERGE_AND_PYRAMID        } from '../../modules/local/merge_and_pyramid'
include { GENERATE_POSTPROCESSING_QC    } from '../../modules/local/generate_postprocessing_qc'

def withDebugView(channel, Closure formatter) {
    return params.debug_channels ? channel.view(formatter) : channel
}

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
    ch_split_output = withDebugView(
        SPLIT_CHANNELS.out.channels,
        { meta, tiffs -> "SPLIT_CHANNELS output: patient=${meta.patient_id}, tiffs=${tiffs*.name}" }
    )

    ch_flatmapped = ch_split_output
        .flatMap { meta, tiffs ->
            // Ensure tiffs is always a list (handle both single file and multiple files)
            def tiff_list = tiffs instanceof List ? tiffs : [tiffs]

            // Create unique meta map for each channel file
            tiff_list.collect { tiff ->
                def channel_meta = meta.clone()
                channel_meta.id = "${meta.patient_id}_${tiff.baseName}"
                channel_meta.channel_name = tiff.baseName
                [channel_meta, tiff]
            }
        }
    ch_flatmapped = withDebugView(
        ch_flatmapped,
        { meta, tiff -> "After flatMap: id=${meta.id}, channel=${meta.channel_name}, tiff=${tiff.name}" }
    )

    ch_for_combine = ch_flatmapped
        .map { meta, tiff -> [meta.patient_id, meta, tiff] }
    ch_for_combine = withDebugView(
        ch_for_combine,
        { patient_id, _meta, _tiff -> "Before combine: key=${patient_id}, channel=${_meta.channel_name}" }
    )

    // Carry BOTH masks (cell + nuclei) keyed by patient_id. The nuclear mask is
    // always available from SEGMENT; QUANTIFY only uses it when
    // params.quantify_compartments is set (per-compartment signal).
    ch_mask = ch_cell_mask
        .map { meta, mask -> [meta.patient_id, mask] }
        .join(ch_nuclei_mask.map { meta, mask -> [meta.patient_id, mask] }, by: 0)
    ch_mask = withDebugView(
        ch_mask,
        { patient_id, _cell, _nuc -> "Masks available: key=${patient_id}, cell=${_cell.name}, nuclei=${_nuc.name}" }
    )

    ch_for_quant = ch_for_combine
        .combine(ch_mask, by: 0)
        .map { _patient_id, meta, tiff, cell_mask, nuclei_mask -> [meta, tiff, cell_mask, nuclei_mask] }
    ch_for_quant = withDebugView(
        ch_for_quant,
        { meta, _tiff, _cell, _nuc -> "After combine: patient=${meta.patient_id}, channel=${meta.channel_name}" }
    )

    QUANTIFY(ch_for_quant)

    // ========================================================================
    // MERGE - Group CSVs by patient_id
    // Deduplicate by patient_id + marker (take first occurrence if same marker appears multiple times)
    // Use groupKey for streaming - emits as soon as channels_count items collected
    // ========================================================================
    ch_grouped_csvs = QUANTIFY.out.individual_csv
        .map { meta, csv ->
            def marker = meta.channel_name  // Extract marker name
            [[meta.patient_id, marker], meta, csv]  // Key by [patient_id, marker]
        }
        .unique { entry -> entry[0] }  // Keep only first occurrence of each [patient_id, marker] pair
        .map { key, meta, csv ->
            // Use groupKey for streaming if channels_count is available
            def gkey = meta.channels_count
                ? groupKey(key[0], meta.channels_count)
                : key[0]
            [gkey, meta, csv]
        }
        .groupTuple(by: 0)
        .map { patient_id, metas, csvs ->
            def meta = metas[0].clone()
            // Extract actual patient_id from groupKey wrapper if needed
            meta.id = patient_id.toString()
            [meta, csvs]
        }

    // Join grouped intensity CSVs with morphology.csv from EXTRACT_CELL_PROPERTIES
    ch_morphology = EXTRACT_CELL_PROPERTIES.out.morphology
        .map { meta, csv -> [meta.patient_id, csv] }

    ch_for_quant_merge = ch_grouped_csvs
        .map { meta, csvs -> [meta.patient_id, meta, csvs] }
        .join(ch_morphology, by: 0)
        .map { _patient_id, meta, csvs, morphology_csv -> [meta, csvs, morphology_csv] }

    MERGE_QUANT_CSVS(ch_for_quant_merge)

    // ========================================================================
    // GEOJSON EXPORT - Export cell data with raw measurements for FlowPath
    // ========================================================================
    // Join merged CSV with pre-computed contours from EXTRACT_CELL_PROPERTIES
    ch_contours = EXTRACT_CELL_PROPERTIES.out.contours
        .map { meta, json_file -> [meta.patient_id, json_file] }

    // Nucleus contours channel: real nucleus contours when compartments are enabled,
    // otherwise reuse the cell contours as a harmless placeholder (EXPORT_GEOJSON does
    // not pass --nucleus_contours_json unless params.quantify_compartments).
    ch_nuc_contours_for_export = params.quantify_compartments ? ch_nucleus_contours : ch_contours

    ch_for_export = MERGE_QUANT_CSVS.out.merged_csv
        .map { meta, csv -> [meta.patient_id, meta, csv] }
        .join(ch_contours, by: 0)
        .join(ch_nuc_contours_for_export, by: 0)
        .map { _patient_id, meta, csv, contours_json, nucleus_contours_json ->
            [meta, csv, contours_json, nucleus_contours_json]
        }

    EXPORT_GEOJSON(ch_for_export)

    // ========================================================================
    // MERGE - Combine split channel TIFFs with segmentation mask (per patient)
    // ========================================================================
    // Group split channel TIFFs by patient for merging
    // SPLIT_CHANNELS already handles DAPI filtering correctly
    // Deduplicate by patient_id + marker to avoid duplicate channel names
    // Use groupKey for streaming - emits as soon as channels_count items collected
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
        .groupTuple(by: 0)
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

    // Join split channels with segmentation mask for MERGE
    ch_for_pyramid_merge = ch_split_grouped
        .map { meta, tiffs -> [meta.patient_id, meta, tiffs] }
        .join(
            ch_cell_mask.map { meta, mask -> [meta.patient_id, mask] },
            by: 0
        )
        .map { _patient_id, meta, split_tiffs, cell_mask ->
            [meta, split_tiffs, cell_mask]
        }

    // MERGE_AND_PYRAMID combines merge + pyramid generation in one step
    // This preserves OME-XML metadata (channel names, colors, pixel sizes)
    // and generates QuPath-compatible pyramidal OME-TIFF directly
    MERGE_AND_PYRAMID(ch_for_pyramid_merge)

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
    ch_base_checkpoint = EXPORT_GEOJSON.out.csv
        .map { meta, csv ->
            def published_path = "${params.outdir}/${meta.patient_id}/geojson/${csv.name}"
            [meta.patient_id, published_path]
        }
        .join(EXPORT_GEOJSON.out.geojson.map { meta, geojson ->
            def published_path = "${params.outdir}/${meta.patient_id}/geojson/${geojson.name}"
            [meta.patient_id, published_path]
        })
        .join(MERGE_QUANT_CSVS.out.merged_csv.map { meta, csv ->
            def published_path = "${params.outdir}/${meta.patient_id}/quantification/${csv.name}"
            [meta.patient_id, published_path]
        })
        .join(ch_cell_mask.map { meta, mask ->
            def published_path = "${params.outdir}/${meta.patient_id}/segmentation/${mask.name}"
            [meta.patient_id, published_path]
        })
        .join(MERGE_AND_PYRAMID.out.pyramid.map { meta, pyramid ->
            def published_path = "${params.outdir}/${meta.patient_id}/pyramid/${pyramid.name}"
            [meta.patient_id, published_path]
        })

    ch_checkpoint_csv = ch_base_checkpoint
        .map { patient_id, cell_csv, cell_geojson, merged_csv, cell_mask, pyramid ->
            "${patient_id},${cell_csv},${cell_geojson},${merged_csv},${cell_mask},${pyramid}"
        }
        .collectFile(
            name: 'postprocessed.csv',
            newLine: true,
            storeDir: "${params.outdir ?: launchDir}/csv",
            seed: 'patient_id,cell_csv,cell_geojson,merged_csv,cell_mask,pyramid'
        )

    // Collect size logs from all postprocessing processes
    ch_size_logs = Channel.empty()
        .mix(SEGMENT.out.size_log)
        .mix(EXTRACT_CELL_PROPERTIES.out.size_log)
        .mix(SPLIT_CHANNELS.out.size_log)
        .mix(QUANTIFY.out.size_log)
        .mix(MERGE_QUANT_CSVS.out.size_log)
        .mix(EXPORT_GEOJSON.out.size_log)
        .mix(MERGE_AND_PYRAMID.out.size_log)

    // Add postprocessing QC size logs if enabled
    if (!params.skip_postprocessing_qc) {
        ch_size_logs = ch_size_logs
            .mix(GENERATE_POSTPROCESSING_QC.out.size_log)
    }

    // Fold in nucleus-properties traces/versions when compartment quantification ran.
    if (params.quantify_compartments) {
        ch_size_logs = ch_size_logs.mix(EXTRACT_NUCLEI_PROPERTIES.out.size_log)
    }

    // Collect versions from all postprocessing processes
    ch_versions = Channel.empty()
        .mix(SEGMENT.out.versions.first())
        .mix(EXTRACT_CELL_PROPERTIES.out.versions.first())
        .mix(SPLIT_CHANNELS.out.versions.first())
        .mix(QUANTIFY.out.versions.first())
        .mix(MERGE_QUANT_CSVS.out.versions.first())
        .mix(EXPORT_GEOJSON.out.versions.first())
        .mix(MERGE_AND_PYRAMID.out.versions.first())

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
