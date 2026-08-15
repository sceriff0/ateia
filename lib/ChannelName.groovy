/*
 * ChannelName - a marker has TWO names, and this class owns the mapping between them.
 *
 *   DECLARED  the samplesheet's spelling ('HLA.DR'). It is the marker's IDENTITY and
 *             the string that fills the <marker> slot of the measurement key
 *             "<marker>: <Compartment>: <Statistic>" that qupath-extension-flowpath
 *             parses case-sensitively (G5).
 *   FILE STEM the sanitised, filesystem-safe form ('HLA_DR'). It names files and
 *             nothing else.
 *
 * WHY THIS CLASS EXISTS. The two forms used to have no owner at all, so identity was
 * reconstructed from the stem: bin/split_multichannel.py sanitised a channel name to
 * build a filename, and subworkflows/local/quantify_markers.nf then set
 * `channel_name = tiff.baseName` -- reading a marker's identity back off disk, already
 * mangled. A panel that declared 'HLA.DR' published 'HLA_DR: Cell: Median', which is
 * not a key the panel's own consumer looks for: bin/phenotype_cells.py builds its
 * lookup from the DECLARED name, misses, and falls through to an all-zero column
 * SILENTLY. Two producers of one contract, disagreeing.
 *
 * ONE SANITISER, TWO LANGUAGES. SPLIT_CHANNELS computes the stems HERE, in Groovy, and
 * passes them to bin/split_multichannel.py with `--file-stems`, so its `script:` and
 * `stub:` paths cannot name the same channel differently -- they read the same answer
 * rather than two sanitisers happening to agree. bin/utils/channel_name.py carries the
 * same rule for standalone invocation and for the OME-metadata path (ADD_CYCLE's
 * SPLIT_PRIOR_PYRAMID passes no `--channels`), and tests/test_channel_identity.py's
 * SANITISER_TABLE is the shared table both halves are held to; the Groovy half is
 * asserted in tests/lib_probe.nf against the same eight rows.
 *
 * The allowlist is ASCII (`[A-Za-z0-9-_]`), deliberately narrower than the Python
 * `str.isalnum()` it replaces: isalnum() is Unicode-aware, so 'beta-catenin' spelled
 * with a Greek beta kept the beta in the filename on one side and could not be
 * reproduced on the other without shipping a Unicode table to Groovy. A rule two
 * languages must agree on cannot depend on a Unicode category.
 */
class ChannelName {

    /**
     * The filesystem-safe stem for ONE declared name.
     *
     * Not unique on its own -- 'CD3.105' and 'CD3_105' both give 'CD3_105'. Use
     * `fileStems` for a list; it disambiguates.
     */
    static String fileStem(def declared) {
        def name = declared == null ? '' : declared.toString()
        def sb = new StringBuilder()
        name.each { ch ->
            def c = ch as char
            sb.append(
                (c >= ('a' as char) && c <= ('z' as char)) ||
                (c >= ('A' as char) && c <= ('Z' as char)) ||
                (c >= ('0' as char) && c <= ('9' as char)) ||
                c == ('-' as char) || c == ('_' as char) ? ch : '_')
        }
        return sb.toString()
    }

    /**
     * Stems for a whole declared list, index-aligned, and UNIQUE.
     *
     * Numbering is by POSITION IN THE DECLARED LIST ('_2', '_3', ...), not by what is
     * already on disk. Disk-order numbering -- what bin/split_multichannel.py did with
     * `os.path.exists` -- gave a different answer depending on which channels were
     * actually written, so a reference slide (nuclear channel kept) and a moving slide
     * (nuclear channel dropped) could number the same collision differently, and the
     * stub, which writes a different set of files again, differently a third time.
     * Position numbering is a pure function of the samplesheet.
     */
    static List<String> fileStems(List declared) {
        def taken = [] as Set
        return (declared ?: []).collect { name ->
            def stem = fileStem(name)
            if (taken.contains(stem)) {
                def suffix = 2
                while (taken.contains("${stem}_${suffix}".toString())) suffix++
                stem = "${stem}_${suffix}".toString()
            }
            taken << stem
            return stem
        }
    }

    /**
     * The declared name a stem came from -- the reverse lookup quantify_markers.nf uses
     * instead of `tiff.baseName`.
     *
     * Falls back to the stem itself when `declared` is empty or holds no match, because
     * the stem is then the best name available and is exactly what the old code used
     * unconditionally. Two callers rely on that: ADD_CYCLE's SPLIT_PRIOR_PYRAMID reads
     * its channel names from OME-XML in REAL mode only (meta.channels is empty under
     * -stub), and a slide may carry a tiff for a marker outside the declared list.
     */
    static String declaredFor(def stem, List declared) {
        def key = stem == null ? '' : stem.toString()
        def names = declared ?: []
        def stems = fileStems(names)
        def hit = stems.findIndexOf { it == key }
        return hit >= 0 ? names[hit].toString() : key
    }

    /**
     * The stems SPLIT_CHANNELS emits for ONE slide -- MarkerUtils.splitOutputChannels'
     * answer, expressed as filenames.
     *
     * The nuclear rule stays MarkerUtils'; this only maps its answer through
     * `fileStems`. Selection is done by INDEX into the full declared list so the
     * disambiguation suffix a channel gets does not depend on `isReference`.
     */
    static List<String> outputStems(List channels, boolean isReference, def nuclearMarkers) {
        def names = channels ?: []
        def stems = fileStems(names)
        def out = []
        names.eachWithIndex { ch, i ->
            if (isReference || !MarkerUtils.isNuclear(ch, nuclearMarkers)) out << stems[i]
        }
        return out
    }

    /**
     * One argument, POSIX-quoted for a Bourne shell.
     *
     * `--channels ${meta.channels.join(' ')}` was unquoted, so a marker named
     * 'CD3 alpha' reached argparse as the two channels 'CD3' and 'alpha' -- which does
     * not fail, it shifts every later channel's NAME onto the wrong pixels. Single
     * quotes suppress every expansion bash does; the only character they cannot contain
     * is a single quote, hence the close-escape-reopen dance.
     */
    static String shellQuote(def value) {
        return "'" + (value == null ? '' : value.toString()).replace("'", "'\\''") + "'"
    }

    /** A space-separated list of quoted arguments, safe to interpolate into a command. */
    static String shellList(List values) {
        return (values ?: []).collect { shellQuote(it) }.join(' ')
    }
}
