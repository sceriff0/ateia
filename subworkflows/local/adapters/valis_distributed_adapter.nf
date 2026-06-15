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

include { REG_PREP }                            from '../../../modules/local/reg_prep'
include { REG_TILE }                            from '../../../modules/local/reg_tile'
include { REG_NONRIGID }                        from '../../../modules/local/reg_nonrigid'
include { REG_NONRIGID as REG_NONRIGID_MICRO }  from '../../../modules/local/reg_nonrigid'
include { REG_MICRO_PREP }                      from '../../../modules/local/reg_micro_prep'
include { REG_FINALIZE }                        from '../../../modules/local/reg_finalize'
include { REG_FINALIZE_FIELD }                  from '../../../modules/local/reg_finalize_field'
include { REG_FINALIZE_MICRO }                  from '../../../modules/local/reg_finalize_micro'
include { REG_WARP_REF }                        from '../../../modules/local/reg_warp_ref'

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

    // ---- NON-RIGID step. Three regimes (spec §6.7 + §5A):
    //   (a) TILED, no micro      : force_tiling=true & skip_micro=true  -> REG_TILE fan-out -> REG_FINALIZE
    //   (b) SEPARATED, no micro   : default                              -> REG_NONRIGID -> REG_FINALIZE_FIELD
    //   (c) SEPARATED + MICRO     : skip_micro=false                     -> + REG_MICRO_PREP -> REG_NONRIGID_MICRO
    //                                                                       -> REG_FINALIZE_MICRO (additive compose)
    // Micro (the heavier 2nd non-rigid pass) ALWAYS uses the separated wave-1 path so its raw field is
    // available to inject; reg_dist_force_tiling only affects wave-1 tiling in the no-micro regime.
    if (params.reg_dist_force_tiling && params.skip_micro_registration) {
        // (a) TILED: fan-out one REG_TILE task per tile, then stitch in REG_FINALIZE.
        ch_tile_tasks = ch_prep_moving.flatMap { key, ti, ws ->
            def n = new groovy.json.JsonSlurper().parseText(ti.resolve('manifest.json').text).n_tiles as int
            (0..<n).collect { i -> tuple(key[0], key[1], ti, i) }
        }
        REG_TILE(ch_tile_tasks)
        ch_tiles = REG_TILE.out.tiles
            .groupTuple(by: [0, 1])
            .map { patient_id, slide, tile_lists -> tuple([patient_id, slide], tile_lists.flatten()) }
        ch_finalize_in = ch_prep_moving
            .join(ch_tiles)
            .join(ch_src.map { key, meta, f -> tuple(key, f) })
            .map { key, ti, ws, tiles, src -> tuple(key[0], key[1], ti, tiles, ws, src) }
        REG_FINALIZE(ch_finalize_in)
        ch_moving_registered = REG_FINALIZE.out.registered
        ch_moving_logs = REG_FINALIZE.out.size_log
    } else {
        // SEPARATED wave-1: whole-image non-rigid in a JVM-free process (bit-identical, low RAM).
        REG_NONRIGID(ch_prep_moving.map { key, ti, ws -> tuple(key[0], key[1], ti) })
        ch_field = REG_NONRIGID.out.field.map { pid, slide, bk -> tuple([pid, slide], bk) }

        if (params.skip_micro_registration) {
            // (b) no micro: compose wave-1 field + warp.
            ch_finalize_in = ch_prep_moving
                .join(ch_field)
                .join(ch_src.map { key, meta, f -> tuple(key, f) })
                .map { key, ti, ws, field, src -> tuple(key[0], key[1], ti, field, ws, src) }
            REG_FINALIZE_FIELD(ch_finalize_in)
            ch_moving_registered = REG_FINALIZE_FIELD.out.registered
            ch_moving_logs = REG_FINALIZE_FIELD.out.size_log
        } else {
            // (c) MICRO second wave (spec §5A Option-2). Gather wave-1 fields per patient -> REG_MICRO_PREP
            // (inject wave-1 field, capture micro 2-D inputs) -> REG_NONRIGID_MICRO (separated, per slide)
            // -> REG_FINALIZE_MICRO (additive compose of the micro residual onto the wave-1 field).
            ch_wave1 = REG_NONRIGID.out.field.groupTuple(by: 0)   // (pid, [slides], [bks])
            ch_micro_prep_in = ch_grouped_meta
                .map { patient_id, ref_item, all_items ->
                    tuple(patient_id, [patient_id: patient_id], ref_item[1],
                          all_items.collect { it[1] }, all_items.collect { it[0] }) }
                .join(REG_PREP.out.prepped.map { pid, prep, metas -> tuple(pid, prep) })
                .join(ch_wave1)
                .map { pid, meta, ref, files, metas, prep, slides, bks ->
                    tuple(meta, pid, ref, files, metas, prep, slides, bks) }
            REG_MICRO_PREP(ch_micro_prep_in)

            // split per-patient micro prep into per-slide micro tiler_inputs + micro_warp_state
            ch_micro_moving = REG_MICRO_PREP.out.prepped.flatMap { pid, mprep, metas ->
                def out = []
                mprep.eachDir { d ->
                    def mti = d.resolve('tiler_inputs')
                    def mws = d.resolve('micro_warp_state.json')
                    if (mti.resolve('manifest.json').exists()) {
                        out << tuple([pid, d.name], mti, mws)
                    }
                }
                out
            }
            REG_NONRIGID_MICRO(ch_micro_moving.map { key, mti, mws -> tuple(key[0], key[1], mti) })
            ch_micro_field = REG_NONRIGID_MICRO.out.field.map { pid, slide, bk -> tuple([pid, slide], bk) }

            ch_fin_micro_in = ch_prep_moving
                .join(ch_field)
                .join(ch_src.map { key, meta, f -> tuple(key, f) })
                .join(ch_micro_moving.map { key, mti, mws -> tuple(key, mti, mws) })
                .join(ch_micro_field)
                .map { key, ti, ws, field, src, mti, mws, mfield ->
                    tuple(key[0], key[1], ti, field, ws, src, mti, mfield, mws) }
            REG_FINALIZE_MICRO(ch_fin_micro_in)
            ch_moving_registered = REG_FINALIZE_MICRO.out.registered
            ch_moving_logs = REG_FINALIZE_MICRO.out.size_log
        }
    }

    // ---- REG_WARP_REF (reference): join ref warp_state + src ----
    ch_ref_in = ch_prep_ref
        .join(ch_src.map { key, meta, f -> tuple(key, f) })
        .map { key, ws, src -> tuple(key[0], key[1], ws, src) }
    REG_WARP_REF(ch_ref_in)

    // ---- convert back to [meta, file] by joining registered outputs to their meta ----
    ch_registered = ch_moving_registered
        .mix(REG_WARP_REF.out.registered)
        .map { patient_id, slide, regfile -> tuple([patient_id, slide], regfile) }
        .join(ch_src.map { key, meta, f -> tuple(key, meta) })
        .map { key, regfile, meta -> tuple(meta, regfile) }

    emit:
    registered = ch_registered
    versions   = REG_PREP.out.versions.first()
    size_logs  = REG_PREP.out.size_log.mix(ch_moving_logs)
}
