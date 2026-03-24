nextflow.enable.dsl = 2

import static ParamUtils.*

/*
========================================================================================
    IMPORT MODULES
========================================================================================
*/

include { SEGMENT                  } from '../../modules/local/segment'
include { EXTRACT_CELL_PROPERTIES } from '../../modules/local/extract_cell_properties'
include { SPLIT_CHANNELS           } from '../../modules/local/split_channels'
include { QUANTIFY                 } from '../../modules/local/quantify'
include { MERGE_QUANT_CSVS         } from '../../modules/local/quantify'
include { EXPORT_GEOJSON            } from '../../modules/local/export_geojson'
include { MERGE_AND_PYRAMID        } from '../../modules/local/merge_and_pyramid'
include { PIXIE_PIXEL_CLUSTER           } from '../../modules/local/pixie_pixel_cluster'
include { PIXIE_CELL_CLUSTER            } from '../../modules/local/pixie_cell_cluster'
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

    SEGMENT(ch_references)

    // ========================================================================
    // CELL PROPERTIES - Extract morphology + contours from mask (runs in PARALLEL with SPLIT_CHANNELS)
    // Computes regionprops ONCE instead of N times in QUANTIFY
    // ========================================================================
    EXTRACT_CELL_PROPERTIES(SEGMENT.out.cell_mask)

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

    ch_mask = SEGMENT.out.cell_mask
        .map { meta, mask -> [meta.patient_id, mask] }
    ch_mask = withDebugView(
        ch_mask,
        { patient_id, _mask -> "Mask available: key=${patient_id}, mask=${_mask.name}" }
    )

    ch_for_quant = ch_for_combine
        .combine(ch_mask, by: 0)
        .map { _patient_id, meta, tiff, mask -> [meta, tiff, mask] }
    ch_for_quant = withDebugView(
        ch_for_quant,
        { meta, _tiff, _mask -> "After combine: patient=${meta.patient_id}, channel=${meta.channel_name}" }
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

    ch_for_merge = ch_grouped_csvs
        .map { meta, csvs -> [meta.patient_id, meta, csvs] }
        .join(ch_morphology, by: 0)
        .map { _patient_id, meta, csvs, morphology_csv -> [meta, csvs, morphology_csv] }

    MERGE_QUANT_CSVS(ch_for_merge)

    // ========================================================================
    // GEOJSON EXPORT - Export cell data with raw measurements for FlowPath
    // ========================================================================
    // Join merged CSV with pre-computed contours from EXTRACT_CELL_PROPERTIES
    ch_contours = EXTRACT_CELL_PROPERTIES.out.contours
        .map { meta, json_file -> [meta.patient_id, json_file] }

    ch_for_export = MERGE_QUANT_CSVS.out.merged_csv
        .map { meta, csv -> [meta.patient_id, meta, csv] }
        .join(ch_contours, by: 0)
        .map { _patient_id, meta, csv, contours_json -> [meta, csv, contours_json] }

    EXPORT_GEOJSON(ch_for_export)

    // ========================================================================
    // PIXIE CLUSTERING (optional, runs in PARALLEL with EXPORT_GEOJSON)
    // Data-driven unsupervised cell clustering using Pixie
    // ========================================================================
    // IMPORTANT: Channel definitions must be OUTSIDE the if block for proper dataflow subscription
    // Only process invocations go inside the if block

    // Convert channels param to list (do this unconditionally for channel definition)
    def pixie_channels_list = ParamUtils.parseListParam(params.pixie_channels)

    def pixie_channel_count = pixie_channels_list.size()

    log.info "PIXIE SETUP: pixie_channels_list=${pixie_channels_list}, count=${pixie_channel_count}, enabled=${params.pixie_enabled}"

    // Define Pixie channel OUTSIDE if block (required for proper dataflow subscription)
    // Uses same proven pattern as ch_split_grouped (which successfully feeds MERGE_AND_PYRAMID)
    ch_for_pixie_pixel = SPLIT_CHANNELS.out.channels
        .flatMap { meta, tiffs ->
            def tiff_list = tiffs instanceof List ? tiffs : [tiffs]
            tiff_list.collect { tiff ->
                [meta.patient_id, tiff.baseName, tiff]
            }
        }
        .filter { _patient_id, marker, _tiff ->
            // Case-insensitive match against pixie_channels_list
            pixie_channels_list.any { ch -> ch.equalsIgnoreCase(marker) }
        }
        .unique { patient_id, marker, _tiff -> [patient_id, marker] }
        .map { patient_id, _marker, tiff -> [patient_id, tiff] }
        .groupTuple(by: 0)  // Simple grouping - no groupKey (which can block)
        .map { patient_id, tiffs ->
            def patient_meta = [
                patient_id: patient_id,
                id: patient_id,
                is_reference: true
            ]
            [patient_id, patient_meta, tiffs]
        }
        .join(
            SEGMENT.out.cell_mask.map { meta, mask -> [meta.patient_id, mask] }
        )
        .map { _patient_id, meta, channel_tiffs, mask ->
            [meta, channel_tiffs, mask]
        }

    if (params.pixie_enabled) {
        // Validate required parameter
        if (!params.pixie_channels || pixie_channels_list.isEmpty()) {
            error "ERROR: params.pixie_channels is required when pixie_enabled=true. " +
                  "Provide a list of channels, e.g.: --pixie_channels \"['CD3','CD4','CD8']\""
        }

        log.info "PIXIE: channels=${pixie_channels_list}, count=${pixie_channel_count}"

        PIXIE_PIXEL_CLUSTER(
            ch_for_pixie_pixel,
            pixie_channels_list
        )

        // Cell clustering needs: pixel data + cluster profiles + cell table + mask + params + tile positions
        // Handle optional tile_positions (not all runs use tiling)
        ch_tile_positions = PIXIE_PIXEL_CLUSTER.out.tile_positions
            .map { meta, positions -> [meta.patient_id, positions] }

        ch_for_pixie_cell = PIXIE_PIXEL_CLUSTER.out.pixel_data
            .map { meta, data -> [meta.patient_id, meta, data] }
            .join(
                PIXIE_PIXEL_CLUSTER.out.cluster_profiles.map { meta, profiles -> [meta.patient_id, profiles] }
            )
            .join(
                MERGE_QUANT_CSVS.out.merged_csv.map { meta, csv -> [meta.patient_id, csv] }
            )
            .join(
                SEGMENT.out.cell_mask.map { meta, mask -> [meta.patient_id, mask] }
            )
            .join(
                PIXIE_PIXEL_CLUSTER.out.cell_params.map { meta, params_file -> [meta.patient_id, params_file] }
            )
            .join(
                ch_tile_positions,
                remainder: true  // Allow missing tile_positions for non-tiled runs
            )
            .map { patient_id, meta, pixel_data, cluster_profiles, cell_table, mask, cell_params, tile_positions ->
                // Handle null tile_positions for non-tiled runs
                def tile_pos = tile_positions ?: file('NO_TILE_POSITIONS')
                [meta, pixel_data, cluster_profiles, cell_table, mask, cell_params, tile_pos]
            }

        PIXIE_CELL_CLUSTER(ch_for_pixie_cell)
    }

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
                patient_id: pid,
                is_reference: false  // Not relevant at patient level
            ]
            [patient_meta, tiffs]
        }

    // Join split channels with segmentation mask for MERGE
    ch_for_merge = ch_split_grouped
        .map { meta, tiffs -> [meta.patient_id, meta, tiffs] }
        .join(
            SEGMENT.out.cell_mask.map { meta, mask -> [meta.patient_id, mask] },
            by: 0
        )
        .map { _patient_id, meta, split_tiffs, cell_mask ->
            [meta, split_tiffs, cell_mask]
        }

    // MERGE_AND_PYRAMID combines merge + pyramid generation in one step
    // This preserves OME-XML metadata (channel names, colors, pixel sizes)
    // and generates QuPath-compatible pyramidal OME-TIFF directly
    MERGE_AND_PYRAMID(ch_for_merge)

    // ========================================================================
    // POSTPROCESSING QC (optional, runs in PARALLEL with MERGE_AND_PYRAMID)
    // ========================================================================
    ch_postprocess_qc = Channel.empty()
    if (!params.skip_postprocessing_qc) {
        // Join cell mask with merged CSV for QC visualization
        ch_for_postprocess_qc = SEGMENT.out.cell_mask
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
        .join(SEGMENT.out.cell_mask.map { meta, mask ->
            def published_path = "${params.outdir}/${meta.patient_id}/segmentation/${mask.name}"
            [meta.patient_id, published_path]
        })
        .join(MERGE_AND_PYRAMID.out.pyramid.map { meta, pyramid ->
            def published_path = "${params.outdir}/${meta.patient_id}/pyramid/${pyramid.name}"
            [meta.patient_id, published_path]
        })

    // Conditionally add Pixie outputs to checkpoint
    if (params.pixie_enabled) {
        ch_checkpoint_csv = ch_base_checkpoint
            .join(PIXIE_CELL_CLUSTER.out.cell_table_clustered.map { meta, csv ->
                def published_path = "${params.outdir}/${meta.patient_id}/pixie/cell_clustering/cell_output/${csv.name}"
                [meta.patient_id, published_path]
            })
            .join(PIXIE_CELL_CLUSTER.out.geojson.map { meta, geojson ->
                def published_path = "${params.outdir}/${meta.patient_id}/pixie/cell_clustering/cell_output/${geojson.name}"
                [meta.patient_id, published_path]
            })
            .join(PIXIE_CELL_CLUSTER.out.mapping_json.map { meta, mapping ->
                def published_path = "${params.outdir}/${meta.patient_id}/pixie/cell_clustering/cell_output/${mapping.name}"
                [meta.patient_id, published_path]
            })
            .map { patient_id, cell_csv, cell_geojson, merged_csv, cell_mask, pyramid, pixie_csv, pixie_geojson, pixie_mapping ->
                "${patient_id},${cell_csv},${cell_geojson},${merged_csv},${cell_mask},${pyramid},${pixie_csv},${pixie_geojson},${pixie_mapping}"
            }
            .collectFile(
                name: 'postprocessed.csv',
                newLine: true,
                storeDir: "./csv",
                seed: 'patient_id,cell_csv,cell_geojson,merged_csv,cell_mask,pyramid,pixie_cell_table,pixie_geojson,pixie_mapping'
            )
    } else {
        ch_checkpoint_csv = ch_base_checkpoint
            .map { patient_id, cell_csv, cell_geojson, merged_csv, cell_mask, pyramid ->
                "${patient_id},${cell_csv},${cell_geojson},${merged_csv},${cell_mask},${pyramid}"
            }
            .collectFile(
                name: 'postprocessed.csv',
                newLine: true,
                storeDir: "./csv",
                seed: 'patient_id,cell_csv,cell_geojson,merged_csv,cell_mask,pyramid'
            )
    }

    // Collect size logs from all postprocessing processes
    ch_size_logs = Channel.empty()
        .mix(SEGMENT.out.size_log)
        .mix(EXTRACT_CELL_PROPERTIES.out.size_log)
        .mix(SPLIT_CHANNELS.out.size_log)
        .mix(QUANTIFY.out.size_log)
        .mix(MERGE_QUANT_CSVS.out.size_log)
        .mix(EXPORT_GEOJSON.out.size_log)
        .mix(MERGE_AND_PYRAMID.out.size_log)

    // Add Pixie size logs if enabled
    if (params.pixie_enabled) {
        ch_size_logs = ch_size_logs
            .mix(PIXIE_PIXEL_CLUSTER.out.size_log)
            .mix(PIXIE_CELL_CLUSTER.out.size_log)
    }

    // Add postprocessing QC size logs if enabled
    if (!params.skip_postprocessing_qc) {
        ch_size_logs = ch_size_logs
            .mix(GENERATE_POSTPROCESSING_QC.out.size_log)
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

    if (params.pixie_enabled) {
        ch_versions = ch_versions
            .mix(PIXIE_PIXEL_CLUSTER.out.versions.first())
            .mix(PIXIE_CELL_CLUSTER.out.versions.first())
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
