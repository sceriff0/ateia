/*
========================================================================================
    VALIS DISTRIBUTED (TILED) REGISTRATION ADAPTER  (spec §6.6)
========================================================================================
    Opt-in low-RAM path that lifts VALIS's in-process non-rigid tile loop into Nextflow
    processes: REG_PREP (rigid + processed-2-D inputs + halt) -> REG_TILE (fan-out, one
    task per tile, JVM-free) -> REG_FINALIZE (fan-in per slide: stitch + compose + warp).
    The reference is warped-to-itself via REG_WARP_REF so downstream QC sees it in the same
    cropped coordinate space as the moving slides (classic VALIS warps every slide).

    Same interface as VALIS_ADAPTER:
      Input:  [patient_id, reference_item, all_items]   (reference_item/all_items = [meta, file])
      Output: registered = Channel of [meta, file]
========================================================================================
*/

include { REG_PREP }     from '../../../modules/local/reg_prep'
include { REG_TILE }     from '../../../modules/local/reg_tile'
include { REG_FINALIZE } from '../../../modules/local/reg_finalize'
include { REG_WARP_REF } from '../../../modules/local/reg_warp_ref'

def slideStem(f) {
    f.name.replaceAll(/\.ome\.tiff?$/, '').replaceAll(/\.tiff?$/, '')
}

workflow VALIS_DISTRIBUTED_ADAPTER {
    take:
    ch_grouped_meta   // [patient_id, reference_item, all_items]

    main:
    // ---- REG_PREP: one invocation per patient (same input shape as the classic adapter) ----
    ch_prep_in = ch_grouped_meta.map { patient_id, ref_item, all_items ->
        def ref_file  = ref_item[1]
        def all_files = all_items.collect { it[1] }
        def all_metas = all_items.collect { it[0] }
        tuple([patient_id: patient_id], patient_id, ref_file, all_files, all_metas)
    }
    REG_PREP(ch_prep_in)

    // ---- per-slide src file + meta, keyed by [patient_id, slide_stem] ----
    ch_src = ch_grouped_meta.flatMap { patient_id, ref_item, all_items ->
        all_items.collect { item -> tuple([patient_id, slideStem(item[1])], item[0], item[1]) }
    }

    // ---- split PREP output into moving slides (have tiler_inputs) and the reference (no tiler_inputs) ----
    ch_prep_moving = REG_PREP.out.prepped.flatMap { patient_id, prep, all_metas ->
        def out = []
        prep.eachDir { d ->
            def ti = d.resolve('tiler_inputs')
            def ws = d.resolve('warp_state.json')
            if (ti.resolve('manifest.json').exists()) {
                out << tuple([patient_id, d.name], ti, ws)
            }
        }
        out
    }
    ch_prep_ref = REG_PREP.out.prepped.flatMap { patient_id, prep, all_metas ->
        def out = []
        prep.eachDir { d ->
            def ti = d.resolve('tiler_inputs')
            def ws = d.resolve('warp_state.json')
            if (!ti.resolve('manifest.json').exists() && ws.exists()) {
                out << tuple([patient_id, d.name], ws)
            }
        }
        out
    }

    // ---- fan-out: read each moving slide's manifest n_tiles -> one REG_TILE task per tile ----
    ch_tile_tasks = ch_prep_moving.flatMap { key, ti, ws ->
        def n = new groovy.json.JsonSlurper().parseText(ti.resolve('manifest.json').text).n_tiles as int
        (0..<n).collect { i -> tuple(key[0], key[1], ti, i) }
    }
    REG_TILE(ch_tile_tasks)

    // ---- fan-in: collect all tile .v files per (patient, slide) ----
    ch_tiles = REG_TILE.out.tiles
        .groupTuple(by: [0, 1])
        .map { patient_id, slide, tile_lists -> tuple([patient_id, slide], tile_lists.flatten()) }

    // ---- REG_FINALIZE (moving): join prep artifacts + tiles + src by [patient_id, slide] ----
    ch_finalize_in = ch_prep_moving
        .join(ch_tiles)                                              // key -> ti, ws, tiles
        .join(ch_src.map { key, meta, f -> tuple(key, f) })          // key -> ..., src
        .map { key, ti, ws, tiles, src -> tuple(key[0], key[1], ti, tiles, ws, src) }
    REG_FINALIZE(ch_finalize_in)

    // ---- REG_WARP_REF (reference): join ref warp_state + src ----
    ch_ref_in = ch_prep_ref
        .join(ch_src.map { key, meta, f -> tuple(key, f) })
        .map { key, ws, src -> tuple(key[0], key[1], ws, src) }
    REG_WARP_REF(ch_ref_in)

    // ---- convert back to [meta, file] by joining registered outputs to their meta ----
    ch_registered = REG_FINALIZE.out.registered
        .mix(REG_WARP_REF.out.registered)
        .map { patient_id, slide, regfile -> tuple([patient_id, slide], regfile) }
        .join(ch_src.map { key, meta, f -> tuple(key, meta) })
        .map { key, regfile, meta -> tuple(meta, regfile) }

    emit:
    registered = ch_registered
    versions   = REG_PREP.out.versions.first()
    size_logs  = REG_PREP.out.size_log.mix(REG_FINALIZE.out.size_log)
}
