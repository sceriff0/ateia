/*
========================================================================================
    VALIS DISTRIBUTED (TILED) REGISTRATION ADAPTER
========================================================================================
    Opt-in low-RAM path that lifts VALIS's in-process non-rigid tile loop into Nextflow
    processes: REG_PREP (rigid + processed-2-D inputs + halt) -> REG_TILE (fan-out, one
    task per tile, JVM-free) -> REG_COMPOSE_* (fan-in per slide: stitch + compose the field)
    -> REG_GRID -> REG_WARP_TILE (fan-out, one task per OUTPUT tile) -> REG_ASSEMBLE.
    The reference goes through the SAME grid/tile/assemble chain with --rigid-only, so downstream QC sees it in the same
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
include { REG_COMPOSE_TILED }                   from '../../../modules/local/reg_compose_tiled'
include { REG_COMPOSE_FIELD }                   from '../../../modules/local/reg_compose_field'
include { REG_COMPOSE_MICRO }                   from '../../../modules/local/reg_compose_micro'
include { REG_GRID }                            from '../../../modules/local/reg_grid'
include { REG_WARP_TILE }                       from '../../../modules/local/reg_warp_tile'
include { REG_ASSEMBLE }                        from '../../../modules/local/reg_assemble'

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
    // NB: keys use patient_id.toString(). patient_id arrives as a groupKey(pid, total_slides) from the
    // main workflow's streaming groupTuple, but REG_PREP's process output round-trips it to a plain
    // String. Mixing groupKey and String in the [pid, slide] join keys makes the joins below silently
    // never match (the compose/warp stages stay pending). Normalize everything to String here.
    ch_src = ch_grouped_meta.flatMap { patient_id, ref_item, all_items ->
        all_items.collect { item -> tuple([patient_id.toString(), slideStem(item[1])], item[0], item[1]) }
    }

    // ---- split PREP output into moving slides (have tiler_inputs) and the reference (no tiler_inputs) ----
    ch_prep_moving = REG_PREP.out.prepped.flatMap { patient_id, prep, all_metas ->
        def out = []
        prep.eachDir { d ->
            def ti = d.resolve('tiler_inputs')
            def ws = d.resolve('warp_state.json')
            if (ti.resolve('manifest.json').exists()) {
                out << tuple([patient_id.toString(), d.name], ti, ws)
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
                out << tuple([patient_id.toString(), d.name], ws)
            }
        }
        out
    }

    // ---- NON-RIGID step. Three regimes:
    //   (a) TILED, no micro      : force_tiling=true & skip_micro=true  -> REG_TILE fan-out -> REG_COMPOSE_TILED
    //   (b) SEPARATED, no micro   : default                              -> REG_NONRIGID -> REG_COMPOSE_FIELD
    //   (c) SEPARATED + MICRO     : skip_micro=false                     -> + REG_MICRO_PREP -> REG_NONRIGID_MICRO
    //                                                                       -> REG_COMPOSE_MICRO (additive compose)
    // Micro (the heavier 2nd non-rigid pass) ALWAYS uses the separated wave-1 path so its raw field is
    // available to inject; reg_dist_force_tiling only affects wave-1 tiling in the no-micro regime.
    if (params.reg_dist_force_tiling && params.skip_micro_registration) {
        // (a) TILED: fan-out one REG_TILE task per tile, then stitch in REG_COMPOSE_TILED.
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
        REG_COMPOSE_TILED(ch_finalize_in)
        ch_moving_field = REG_COMPOSE_TILED.out.field
        ch_moving_logs = REG_COMPOSE_TILED.out.size_log
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
            REG_COMPOSE_FIELD(ch_finalize_in)
            ch_moving_field = REG_COMPOSE_FIELD.out.field
            ch_moving_logs = REG_COMPOSE_FIELD.out.size_log
        } else {
            // (c) MICRO second wave. Gather wave-1 fields per patient -> REG_MICRO_PREP
            // (inject wave-1 field, capture micro 2-D inputs) -> REG_NONRIGID_MICRO (separated, per slide)
            // -> REG_COMPOSE_MICRO (additive compose of the micro residual onto the wave-1 field).
            // NB: patient_id is a groupKey(pid, total_slides_incl_ref) from the streaming groupTuple in
            // the main workflow. REG_NONRIGID only emits MOVING-slide fields (the ref is rigid-only),
            // so groupTuple by the groupKey would wait for the ref field that never arrives and STALL.
            // Strip to a plain String key so groupTuple waits for channel close, and normalize all join
            // keys to String so they match.
            ch_wave1 = REG_NONRIGID.out.field
                .map { pid, slide, bk -> tuple(pid.toString(), slide, bk) }
                .groupTuple(by: 0)   // (pid, [slides], [bks]) — moving slides only
            ch_micro_prep_in = ch_grouped_meta
                .map { patient_id, ref_item, all_items ->
                    tuple(patient_id.toString(), [patient_id: patient_id], ref_item[1],
                          all_items.collect { it[1] }, all_items.collect { it[0] }) }
                .join(REG_PREP.out.prepped.map { pid, prep, metas -> tuple(pid.toString(), prep) })
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

            // Normalize the [pid, slide] keys to plain Strings on every input: ch_prep_moving/ch_field/
            // ch_src carry the groupKey pid, while ch_micro_moving/ch_micro_field carry the String pid
            // (REG_MICRO_PREP was fed pid.toString()). List keys [groupKey,slide] != [String,slide] would
            // never join, leaving REG_COMPOSE_MICRO pending.
            ch_fin_micro_in = ch_prep_moving.map { key, ti, ws -> tuple([key[0].toString(), key[1]], ti, ws) }
                .join(ch_field.map { key, f -> tuple([key[0].toString(), key[1]], f) })
                .join(ch_src.map { key, meta, f -> tuple([key[0].toString(), key[1]], f) })
                .join(ch_micro_moving.map { key, mti, mws -> tuple([key[0].toString(), key[1]], mti, mws) })
                .join(ch_micro_field.map { key, bk -> tuple([key[0].toString(), key[1]], bk) })
                .map { key, ti, ws, field, src, mti, mws, mfield ->
                    tuple(key[0], key[1], ti, field, ws, src, mti, mfield, mws) }
            REG_COMPOSE_MICRO(ch_fin_micro_in)
            ch_moving_field = REG_COMPOSE_MICRO.out.field
            ch_moving_logs = REG_COMPOSE_MICRO.out.size_log
        }
    }

    // ---- FULL-RES WARP (moving slides + the reference, ONE shared fan-out) ----
    // Keys normalised to String throughout: ch_prep_moving/ch_prep_ref/ch_src carry the groupKey
    // patient_id from the main workflow's streaming groupTuple, while process outputs carry a plain
    // String. A join key mixing the two never matches, and the symptom is not an error -- the
    // downstream process simply stays pending forever. See the note at line 49.
    ch_key_src = ch_src.map { key, meta, f -> tuple([key[0].toString(), key[1]], f) }
    ch_key_ws  = ch_prep_moving.map { key, ti, ws -> tuple([key[0].toString(), key[1]], ws) }

    ch_moving_warp = ch_moving_field
        .map { pid, slide, field -> tuple([pid.toString(), slide], field) }
        .join(ch_key_ws)
        .join(ch_key_src)
        .map { key, field, ws, src -> tuple(key[0], key[1], ws, src, field) }

    // The reference warps with its rigid M + crop only: field == [] selects --rigid-only. Classic
    // VALIS warps every slide including the reference, so downstream QC needs it in the same
    // cropped coordinate space.
    ch_ref_warp = ch_prep_ref
        .map { key, ws -> tuple([key[0].toString(), key[1]], ws) }
        .join(ch_key_src)
        .map { key, ws, src -> tuple(key[0], key[1], ws, src, []) }

    WARP_FANOUT(ch_moving_warp.mix(ch_ref_warp))

    // ---- convert back to [meta, file] by joining registered outputs to their meta ----
    ch_registered = WARP_FANOUT.out.registered
        .map { pid, slide, regfile -> tuple([pid.toString(), slide], regfile) }
        .join(ch_src.map { key, meta, f -> tuple([key[0].toString(), key[1]], meta) })
        .map { key, regfile, meta -> tuple(meta, regfile) }

    emit:
    registered = ch_registered
    versions   = REG_PREP.out.versions.first()
    size_logs  = REG_PREP.out.size_log.mix(ch_moving_logs).mix(WARP_FANOUT.out.size_log)
}


// Shared full-res warp: grid -> per-tile warp -> assemble. Every regime (tiled non-rigid,
// separated, separated+micro) AND the reference route through this, so the pipeline contains
// exactly ONE full-res warp implementation -- the one that is bit-identical to the single-process
// warp (verified by the integration tests).
//
// Called exactly ONCE: a DSL2 workflow cannot be invoked twice without aliasing, which is why the
// moving slides and the reference are mixed into a single channel above.
workflow WARP_FANOUT {
    take:
    ch_in   // [pid, slide, warp_state, src_slide, field]  (field == [] means rigid-only)

    main:
    ch_norm = ch_in.map { pid, slide, ws, src, field ->
        tuple(pid.toString(), slide, ws, src, field)
    }

    REG_GRID(ch_norm)

    // Re-join the grid to its inputs, then emit one task per tile. The tile COUNT comes from
    // grid.json, which REG_GRID derived from the real warped canvas -- not from warp_state
    // arithmetic, which could disagree with what REG_WARP_TILE actually produces.
    ch_with_grid = ch_norm
        .map { pid, slide, ws, src, field -> tuple([pid, slide], ws, src, field) }
        .join(REG_GRID.out.grid.map { pid, slide, g -> tuple([pid.toString(), slide], g) })

    ch_tasks = ch_with_grid.flatMap { key, ws, src, field, grid ->
        def n = new groovy.json.JsonSlurper().parseText(grid.text).tiles.size()
        (0..<n).collect { i -> tuple(key[0], key[1], ws, src, field, grid, i) }
    }
    REG_WARP_TILE(ch_tasks)

    // Fan-in. groupTuple with no size waits for channel close, which is correct here: the tile
    // count per slide is only known after REG_GRID, so there is no size to supply up front.
    ch_tiles = REG_WARP_TILE.out.tile
        .map { pid, slide, t -> tuple([pid.toString(), slide], t) }
        .groupTuple()
        .map { key, tl -> tuple(key, tl.flatten()) }

    ch_assemble_in = ch_with_grid
        .map { key, ws, src, field, grid -> tuple(key, ws, src, grid) }
        .join(ch_tiles)
        .map { key, ws, src, grid, tiles -> tuple(key[0], key[1], ws, src, grid, tiles) }
    REG_ASSEMBLE(ch_assemble_in)

    emit:
    registered = REG_ASSEMBLE.out.registered
    versions   = REG_ASSEMBLE.out.versions
    size_log   = REG_ASSEMBLE.out.size_log
}
