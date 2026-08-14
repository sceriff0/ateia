/*
 * TilePlan - the STARE tile-plan CSV schema, for the Groovy/Nextflow layer.
 *
 * The tile plan is the artifact TILED_COARSE writes, the Nextflow fan-out splits row by
 * row, and TILED_REG_TILE reads back as CLI flags. Its column list used to be written out
 * by hand in three independent places: bin/tiled_coarse.py's `csv.writerow`, TILED_COARSE's
 * stub `printf`, and TILED_REG_TILE's field-by-field `${row.rx0}` interpolation. Nothing
 * tied them together, so renaming or reordering a column surfaced either as a stub that no
 * longer matched the real artifact, or as `--rx0 null` in a rendered command -- neither of
 * which fails loudly.
 *
 * There are necessarily TWO owners, because the writer is Python and the stub/consumer are
 * Groovy: this class and `bin/utils/tile_grid.py`'s TILE_PLAN_COLUMNS. They are held equal
 * by tests/test_tile_plan_schema.py, which also refuses any third restatement.
 *
 * A COLUMN NAME IS A FLAG NAME. `regTileArgs` renders `--<column> <value>` straight from
 * REG_TILE_COLUMNS, so renaming a column renames the CLI contract; the same guard test
 * asserts every rendered flag is one bin/tiled_reg_tile.py actually declares.
 *
 * (This class is reachable from a module's `script:`/`stub:` block, but NOT from a
 * conf/*.config closure -- see CLAUDE.md, "Config-driven args".)
 */
class TilePlan {

    /** Every column of the tile-plan CSV, in write order. */
    static final List<String> COLUMNS = [
        'ix', 'iy', 'cx', 'cy',
        'x0', 'y0', 'x1', 'y1',
        'rx0', 'ry0', 'rx1', 'ry1'
    ]

    /**
     * The subset TILED_REG_TILE passes to bin/tiled_reg_tile.py: the tile's grid position,
     * its control-point centre, and its READ box (core + halo). The core box (x0..y1) is
     * the stitch's business, not the per-tile residual's, so it is deliberately not sent.
     */
    static final List<String> REG_TILE_COLUMNS = [
        'ix', 'iy', 'cx', 'cy', 'rx0', 'ry0', 'rx1', 'ry1'
    ]

    /**
     * The single tile the stubs publish: one 16x16 tile covering a 16x16 frame. Declared as
     * a column->value map rather than a comma string so it cannot fall out of step with
     * COLUMNS' order, and so a new column makes the stub fail rather than silently emit a
     * short row.
     */
    static final Map STUB_TILE = [
        ix: 0, iy: 0, cx: 8, cy: 8,
        x0: 0, y0: 0, x1: 16, y1: 16,
        rx0: 0, ry0: 0, rx1: 16, ry1: 16
    ]

    /** The CSV header line. */
    static String header() {
        return COLUMNS.join(',')
    }

    /** One CSV row, fields ordered by COLUMNS. Throws on a column the map does not carry. */
    static String row(Map values) {
        return COLUMNS.collect {
            if (!values.containsKey(it))
                throw new IllegalArgumentException("tile-plan row is missing column '${it}'")
            values[it]
        }.join(',')
    }

    /**
     * The per-tile CLI flags for bin/tiled_reg_tile.py, rendered from a tile-plan CSV row
     * as `splitCsv(header: true)` yields it.
     */
    static String regTileArgs(Map row) {
        return REG_TILE_COLUMNS.collect {
            if (row[it] == null)
                throw new IllegalArgumentException(
                    "tile-plan row has no '${it}' column: ${row}. The plan written by " +
                    "bin/tiled_coarse.py and TilePlan.COLUMNS have diverged.")
            "--${it} ${row[it]}"
        }.join(' ')
    }
}
