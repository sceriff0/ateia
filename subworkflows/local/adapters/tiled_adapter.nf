/*
========================================================================================
    TILED (STARE) REGISTRATION ADAPTER
========================================================================================
    Converts the patient-grouped structure into the tiled method's per-moving-slide star:
    every moving slide registers directly to the fixed reference (which defines the frame),
    independently and in parallel. Unlike the VALIS adapter there is no batch graph build and
    no per-slide OME-channel re-matching — each task already carries its slide's meta.

    Two execution modes (params.reg_tiled_fanout), same channel contract as VALIS_ADAPTER:
      * false (default): one TILED_REGISTER task per moving slide, tiled internally (≤8 GB).
      * true:            per-TILE Nextflow fan-out — TILED_COARSE -> TILED_REG_TILE (one task
                         per tile) -> TILED_SOLVE -> TILED_STITCH. Maximises parallelism.

    Input:  ch_grouped_meta - Channel of [patient_id, reference_item, all_items]
========================================================================================
*/

include { TILED_REGISTER } from '../../../modules/local/tiled_register'
include { TILED_COARSE   } from '../../../modules/local/tiled_coarse'
include { TILED_REG_TILE } from '../../../modules/local/tiled_reg_tile'
include { TILED_SOLVE    } from '../../../modules/local/tiled_solve'
include { TILED_STITCH   } from '../../../modules/local/tiled_stitch'

// Stable per-moving-slide join key (patient + its channel set).
def slideKey(meta) { "${meta.patient_id}#${meta.channels.toSorted().join('_')}" }

workflow TILED_ADAPTER {
    take:
    ch_grouped_meta   // [patient_id, reference_item, all_items]

    main:
    // The reference is the frame — it passes through unregistered.
    ch_reference = ch_grouped_meta.map { _pid, ref_item, _items -> ref_item }  // [meta, file]

    // One stream item per moving (non-reference) slide: [meta, reference_file, moving_file].
    ch_moving = ch_grouped_meta.flatMap { _pid, ref_item, items ->
        def ref_file = ref_item[1]
        items.findAll { item -> !item[0].is_reference }
             .collect { mov -> tuple(mov[0], ref_file, mov[1]) }
    }

    if (params.reg_tiled_fanout) {
        TILED_COARSE(ch_moving)

        // Context per slide: meta + reference + moving + M0, keyed for the per-tile join.
        ch_ctx = ch_moving
            .map { meta, ref, mov -> tuple(slideKey(meta), meta, ref, mov) }
            .join(TILED_COARSE.out.m0.map { meta, m0 -> tuple(slideKey(meta), m0) }, by: 0)

        // Fan out: one item per tile (splitCsv), attach the slide context.
        ch_tile_items = TILED_COARSE.out.tiles
            .splitCsv(header: true, elem: 1)
            .map { meta, row -> tuple(slideKey(meta), row) }
            .combine(ch_ctx, by: 0)
            .map { _k, row, meta, ref, mov, m0 -> tuple(meta, m0, ref, mov, row) }

        TILED_REG_TILE(ch_tile_items)

        // Gather every tile's control point back per slide.
        ch_controls = TILED_REG_TILE.out.control
            .map { meta, c -> tuple(slideKey(meta), meta, c) }
            .groupTuple(by: 0)
            .map { _k, metas, controls -> tuple(metas[0], controls) }

        ch_solve_in = ch_controls
            .map { meta, controls -> tuple(slideKey(meta), meta, controls) }
            .join(TILED_COARSE.out.m0.map { meta, m0 -> tuple(slideKey(meta), m0) }, by: 0)
            .map { _k, meta, controls, m0 -> tuple(meta, m0, controls) }

        TILED_SOLVE(ch_solve_in)

        ch_stitch_in = TILED_SOLVE.out.manifest
            .map { meta, m -> tuple(slideKey(meta), meta, m) }
            .join(ch_moving.map { meta, _ref, mov -> tuple(slideKey(meta), mov) }, by: 0)
            .map { _k, meta, m, mov -> tuple(meta, m, mov) }

        TILED_STITCH(ch_stitch_in)

        ch_registered_moving = TILED_STITCH.out.registered
        ch_manifest_by_meta  = TILED_SOLVE.out.manifest
        ch_size_logs         = TILED_STITCH.out.size_log
        ch_versions          = TILED_COARSE.out.versions.first()
            .mix(TILED_REG_TILE.out.versions.first())
            .mix(TILED_SOLVE.out.versions.first())
            .mix(TILED_STITCH.out.versions.first())
        ch_summary           = TILED_SOLVE.out.tre.map { _meta, f -> f }
    } else {
        TILED_REGISTER(ch_moving)
        ch_registered_moving = TILED_REGISTER.out.registered
        ch_manifest_by_meta  = TILED_REGISTER.out.manifest
        ch_size_logs         = TILED_REGISTER.out.size_log
        ch_versions          = TILED_REGISTER.out.versions.first()
        ch_summary           = TILED_REGISTER.out.tre.map { _meta, f -> f }
    }

    ch_registered = ch_registered_moving.mix(ch_reference)

    emit:
    registered       = ch_registered
    // Manifest keyed by patient, mirroring VALIS_ADAPTER.out.registrar.
    manifest         = ch_manifest_by_meta.map { meta, m -> tuple(meta.patient_id, m) }
    // Same manifests keyed by meta — the reg_qc=2 seg-QC joins one manifest per moving slide.
    manifest_by_meta = ch_manifest_by_meta
    // The tiled method composes no stages destructively, so it needs no pre-micro checkpoint.
    stage_checkpoint = Channel.empty()
    size_logs        = ch_size_logs
    versions         = ch_versions
    summary          = ch_summary
}
