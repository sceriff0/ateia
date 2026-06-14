/*
 * REG_TILE - distributed VALIS tiled registration, stage 2 (fan-out, one task per tile)
 *
 * Computes ONE tile's bk/fwd displacement field with VALIS's own reg_tile kernel, JVM-free (~1-2 GB).
 * Proven bit-identical to the in-process loop (spec §6.1, Task 6). Reads REG_PREP's per-slide
 * tiler_inputs dir; emits bk_<idx>.v / fwd_<idx>.v.
 */
process REG_TILE {
    tag "${patient_id}:${slide}:${tile_idx}"
    label 'process_low'

    container "${params.reg_dist_container ?: 'mirage-valis:1.0.0'}"

    input:
    tuple val(patient_id), val(slide), path(inputs_dir, stageAs: 'tiler_inputs'), val(tile_idx)

    output:
    tuple val(patient_id), val(slide), path("tiles/*.v"), emit: tiles
    path "versions.yml"                                 , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    """
    mkdir -p tiles
    reg_tile.py --inputs-dir tiler_inputs --tile-idx ${tile_idx} --out-dir tiles

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        valis: \$(python -c "import valis; print(valis.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    """
    mkdir -p tiles
    touch tiles/bk_${tile_idx}.v tiles/fwd_${tile_idx}.v
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        valis: stub
    END_VERSIONS
    """
}
