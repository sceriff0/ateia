/*
========================================================================================
    IMPORT MODULES
========================================================================================
*/

include { CONVERT_IMAGE           } from '../../modules/local/convert_image'
include { PREPROCESS              } from '../../modules/local/preprocess'
include { GENERATE_PREPROCESS_QC  } from '../../modules/local/generate_preprocess_qc'

/*
========================================================================================
    SUBWORKFLOW: PREPROCESSING
========================================================================================
    Description:
        Converts images to standardized OME-TIFF (with the nuclear/fiducial channel
        in channel 0) and applies BaSiC illumination correction. The correction is
        skipped entirely when params.skip_preprocessing is set; conversion is not,
        because everything downstream assumes the standardised layout.

    Input:
        ch_input: Channel of [meta, file] tuples where meta contains:
                  - patient_id: Patient identifier
                  - is_reference: Boolean indicating reference image
                  - channels: List of channel names (the nuclear/fiducial
                    marker is moved to index 0 by CONVERT_IMAGE)

    Output:
        preprocessed: Preprocessed OME-TIFF images with [meta, file] tuples
        checkpoint_csv: CSV for restart capability
========================================================================================
*/

workflow PREPROCESSING {
    take:
    ch_input  // Channel of [meta, file] tuples

    main:
    // Convert to standardized OME-TIFF format
    CONVERT_IMAGE ( ch_input )

    // Parse output channels from channels.txt file and update metadata
    ch_for_preprocess = CONVERT_IMAGE.out.ome_tiff
        .map { meta, ome_file, channels_file ->
            // Read output channels from file (the nuclear/fiducial channel is first)
            def output_channels = channels_file.text.trim().split(',').toList()

            // Update meta with output channel order. This is safe from the
            // aliasing hazard described in valis_adapter.nf:82-88 not because
            // `+` avoids aliasing -- it doesn't; Map.plus() is
            // cloneSimilarMap(left).putAll(right), operationally identical to
            // clone() -- but because `channels` is rebound here to a
            // brand-new List (output_channels, freshly built by
            // .split(',').toList()) rather than to the original
            // meta.channels reference.
            def updated_meta = meta + [channels: output_channels]
            [updated_meta, ome_file]
        }

    // Preprocess each file (BaSiC correction), unless it is switched off entirely.
    //
    // CONVERT_IMAGE always runs: it is what standardises the input to OME-TIFF and
    // moves the nuclear/fiducial channel to index 0, which every downstream step
    // assumes. `skip_preprocessing` turns off the BaSiC CORRECTION only, so the step
    // still emits one image per input and the checkpoint still has a row per slide --
    // the row just points at the converted image instead of a corrected one.
    //
    // This is a branch and not an `ext.when` because the checkpoint must name the
    // directory the file was ACTUALLY published into. PREPROCESS publishes to
    // <pid>/preprocessed/, CONVERT_IMAGE to <pid>/converted/. Recording the
    // preprocessed path for a run that never ran PREPROCESS would name a directory
    // that does not exist on disk -- the same defect Layout.passthroughPath documents
    // for unregistered slides, and one tests/checkpoint_manifest.nf.test fails on.
    if (params.skip_preprocessing) {
        ch_preprocessed_with_meta = ch_for_preprocess
    } else {
        PREPROCESS ( ch_for_preprocess )
        ch_preprocessed_with_meta = PREPROCESS.out.preprocessed
    }

    // Generate QC images (PNG per channel) for visual inspection
    if (!params.skip_preprocess_qc) {
        GENERATE_PREPROCESS_QC ( ch_preprocessed_with_meta )
    }

    // Generate checkpoint CSV for restart from preprocessing step
    // Use collectFile() for non-blocking aggregation (enables patient-level parallelism)
    ch_checkpoint_csv = ch_preprocessed_with_meta
        .map { meta, image_file ->
            Checkpoint.row(Layout.PREPROCESSED, [
                patient_id        : meta.patient_id,
                // Both kinds are spelled out as LITERAL arguments rather than resolved
                // through a variable: tests/test_layout.py statically scans for
                // `Layout.publishedPath(params.*, <pid>, <kind>)` and pins that
                // 'preprocessed' is still among the kinds this pipeline asks for. A
                // variable here is invisible to that scan, so the guard would silently
                // stop covering the checkpoint that a `--start registration` run reads.
                preprocessed_image: params.skip_preprocessing
                    ? Layout.publishedPath(params.outdir, meta.patient_id, 'converted', image_file)
                    : Layout.publishedPath(params.outdir, meta.patient_id, Layout.PREPROCESSED, image_file),
                is_reference      : meta.is_reference,
                channels          : meta.channels.join('|'),
            ])
        }
        .collectFile(
            name: Layout.checkpointCsvName(Layout.PREPROCESSED),
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
            seed: Checkpoint.header(Layout.PREPROCESSED)
        )

    // Collect size logs from all processes
    ch_size_logs = Channel.empty()
        .mix(CONVERT_IMAGE.out.size_log)

    // Collect versions from all processes
    ch_versions = Channel.empty()
        .mix(CONVERT_IMAGE.out.versions.first())

    if (!params.skip_preprocessing) {
        ch_size_logs = ch_size_logs.mix(PREPROCESS.out.size_log)
        ch_versions  = ch_versions.mix(PREPROCESS.out.versions.first())
    }

    // Collect QC outputs (if enabled)
    ch_preprocess_qc = Channel.empty()
    if (!params.skip_preprocess_qc) {
        ch_preprocess_qc = GENERATE_PREPROCESS_QC.out.qc.map { meta, pngs -> pngs }
        ch_size_logs = ch_size_logs.mix(GENERATE_PREPROCESS_QC.out.size_log)
        ch_versions = ch_versions.mix(GENERATE_PREPROCESS_QC.out.versions.first())
    }

    emit:
    preprocessed   = ch_preprocessed_with_meta
    checkpoint_csv = ch_checkpoint_csv
    preprocess_qc  = ch_preprocess_qc
    size_logs      = ch_size_logs
    versions       = ch_versions
}
