/*
========================================================================================
    ProcessEnvelope — the versions.yml boilerplate, rendered once
========================================================================================
    Every process in modules/local/ ends its script: block with a versions.yml heredoc,
    and 21 of them begin it with a *.size.csv row. This class renders BOTH, for both
    blocks, from one call each. The size-log half was hand-written 21 times in script:
    and 21 times in stub:, and the two halves were not comparable: the stub copies wrote
    the literal `STUB` as the process name, and the byte computation had grown ~25
    spellings across the 21 (three `find ... -printf` variants, `du -sLb`, and
    `stat || echo 0`), three of which yielded an empty string rather than 0.
    The versions.yml heredoc was written out by hand in every module, and written TWICE
    per module — once in script:, once in stub: with every value replaced by the
    literal `stub`.

    WHY THAT WAS DANGEROUS RATHER THAN MERELY REPETITIVE. `-stub` never evaluates a
    script: block. So the stub copy is not a mirror the test suite compares against the
    real one — it is a second, independently maintained list that the entire blocking CI
    gate cannot see diverge. A tool added to script: and forgotten in stub: produces a
    versions.yml that reports fewer tools under -stub than in a real run, silently.

    Rendering both from ONE list per module removes the second copy entirely: the stub
    block asks for the same `tools` list the script block does.

    SEGMENT.NF IS A CONSUMER, NOT THE PRECEDENT. It used to be the precedent:
    lib/SegBackends.groovy held each backend's version rows PRE-RENDERED and
    modules/local/segment.nf spliced them in. That relationship is now inverted —
    SegBackends stores bare module NAMES (`versionTools`, the same key
    lib/WarpBackends.groovy uses) and this class renders them, so segment.nf goes
    through ProcessEnvelope exactly like every other module. It is only unusual in
    that its tool list is chosen at runtime from params.seg_method rather than
    written literally at the call site.

    INDENTATION IS THE CALLER'S PROBLEM, AND THAT IS DELIBERATE. Nextflow applies
    stripIndent() to the finished script, so a pre-indented block from here would fight
    the caller's own indentation. Every method returns lines joined with '\n' at column
    zero; the caller interpolates it where the heredoc belongs. Same rule as
    SegBackends' shell fragments.

    All methods are static; nothing here reads params.
========================================================================================
*/

class ProcessEnvelope {

    /*
     * Python module name -> the YAML key the QC report expects. Only modules whose import
     * name differs from their reported name need an entry; everything else reports under
     * its own name.
     *
     * These strings are the KEYS of every published versions.yml, collated into the
     * report's mirage_qc_data_<run-id> bundle as collated_versions.yml. The HTML QC
     * report no longer renders them as a table (it never read them well: the parser
     * was hand-rolled and two-level), so changing a value here changes a published
     * DATA file rather than a published rendering — which is the harder break,
     * because the consumer is a script.
     */
    private static final Map<String, String> YAML_KEY = [
        'skimage'   : 'scikit-image',
        'sklearn'   : 'scikit-learn',
        'PIL'       : 'pillow',
        'yaml'      : 'pyyaml',
    ].asImmutable()

    private static String yamlKey(String tool) {
        // NOT YAML_KEY.get(tool, tool): Groovy's DefaultGroovyMethods.get(Map, key, default)
        // extension inserts the default into the map when the key is absent (a put(), not a
        // read) and throws UnsupportedOperationException against an immutable map. getOrDefault
        // is the plain java.util.Map method and never mutates.
        return YAML_KEY.getOrDefault(tool, tool)
    }

    /** The version-probe line for one tool, as it appears inside the heredoc. */
    private static String probe(String tool) {
        // ONE level of escaping, not two. `${...}` splices this returned String verbatim
        // into the caller's """...""" GString -- it is not re-parsed for Groovy escapes.
        // A single `\$` in THIS source produces a bare `$` in the returned string (Groovy's
        // GString escape for "literal dollar, don't interpolate"), which is what needs to
        // land in .command.sh: `<<-END_VERSIONS` is an UNQUOTED heredoc, so bash performs
        // command substitution on a bare `$(...)`. Writing `\\\$` here (two escapes) instead
        // produces a literal backslash-dollar in the returned string, which survives into
        // .command.sh as `\$(...)` -- an ESCAPED dollar that bash prints as literal text
        // instead of executing. That shipped once: every real (non-stub) run wrote the
        // command text itself into versions.yml instead of a version number, and nothing
        // caught it because `-stub` never evaluates script: and bin/generate_qc_report.py's
        // parser only splits on ":" -- it never validates what it finds.
        return "    ${yamlKey(tool)}: \$(python -c \"import ${tool}; print(${tool}.__version__)\" 2>/dev/null || echo \"unknown\")"
    }

    /**
     * The full versions.yml heredoc for a `script:` block.
     *
     * `python:` is prepended automatically — at the time this class was introduced, 27
     * of the then-28 modules reported it (`aggregate_size_logs.nf`, bash-only, was the
     * sole exception; two others called `python3` instead of `python` — still a
     * `python:` report, just a different interpreter name, not a non-report). Four of
     * those 28 modules have since gone (`compile_panel.nf`, `phenotype.nf`,
     * `tiled_register.nf`, `warp_seg_qc_tiled.nf`), so the count as of this comment is
     * 23 of 24 — still the one bash-only exception. Pass tools in the order they should
     * appear in the report.
     */
    static String versions(String process, List<String> tools) {
        def lines = ['cat <<-END_VERSIONS > versions.yml',
                     "\"${process}\":",
                     // Single-quoted Groovy string: `$` has no interpolation meaning here
                     // at all, so it needs no escaping -- a bare `$(...)` is exactly what
                     // must land in .command.sh for bash to execute it. See probe()'s
                     // comment for what happens when this is over-escaped instead.
                     '    python: $(python --version 2>&1 | sed \'s/Python //\')']
        lines += tools.collect { probe(it) }
        lines << 'END_VERSIONS'
        return lines.join('\n')
    }

    /**
     * The same heredoc for a `stub:` block, every value the literal `stub`.
     *
     * Takes the SAME tools list as versions(). That is the whole point: the two blocks
     * can no longer name different tools.
     */
    static String versionsStub(String process, List<String> tools) {
        def lines = ['cat <<-END_VERSIONS > versions.yml',
                     "\"${process}\":",
                     '    python: stub']
        lines += tools.collect { "    ${yamlKey(it)}: stub" }
        lines << 'END_VERSIONS'
        return lines.join('\n')
    }

    /**
     * Column order of every *.size.csv row and of AGGREGATE_SIZE_LOGS' header.
     *
     * THE OWNER OF THE ORDER, not a description of it: sizeLog/sizeLogStub build
     * their row by looking each column up in this list, and
     * modules/local/aggregate_size_logs.nf renders its header with
     * ${ProcessEnvelope.SIZE_LOG_COLUMNS.join(',')}. Reordering here reorders
     * both. bin/generate_resource_report.py declares the same tuple and
     * tests/test_size_log_schema_has_one_owner.py fails if the two drift.
     */
    static final List<String> SIZE_LOG_COLUMNS = ['process', 'sample_id', 'filename', 'bytes'].asImmutable()

    /**
     * The `filename` cell for a row: the basename of the sole path, or the literal
     * `inputs/` when the measurement covers more than one file.
     *
     * A glob is more than one file by construction, so it takes `inputs/` too --
     * naming a row after its own wildcard ('*', from 'channels/*') would be worse
     * than saying nothing. A staged prefix is dropped ('mov/x.tif' -> 'x.tif')
     * because stageAs positioning is not part of the file's identity, and because
     * that is exactly what the hand-written rows interpolated (`${moving.name}`).
     */
    private static String filenameCell(List<String> shellPaths) {
        if (shellPaths.size() != 1) { return 'inputs/' }
        def p = shellPaths[0]
        if (p.contains('*') || p.contains('?')) { return 'inputs/' }
        def slash = p.lastIndexOf('/')
        return slash < 0 ? p : p.substring(slash + 1)
    }

    private static String row(Map cells, String outFile) {
        return 'echo "' + SIZE_LOG_COLUMNS.collect { cells[it] }.join(',') + '" > ' + outFile
    }

    /**
     * The size-log lines for a `script:` block: one `stat` of every path in
     * `shellPaths`, summed, then ONE row into `outFile`.
     *
     * `shellPaths` entries are text that reaches BASH. A module passes a staged
     * input as "${image_file}" -- DOUBLE quotes, so the call site interpolates it
     * to the staged name before this method ever sees it -- or a literal glob as
     * 'channels/*'. Single-quoting a `${...}` at the call site would hand this
     * method the four characters `${x}`, which land verbatim in .command.sh and
     * expand to an UNSET SHELL VARIABLE: an empty stat operand and a silent zero.
     *
     * Every path is stat'ed in ONE invocation and summed by awk, replacing the ~25
     * spellings the modules had grown (`stat || echo 0`, `du -sLb`, three different
     * `find ... -printf` forms), three of which returned an empty string rather
     * than 0 for a missing file and relied on a `${x:-0}` at the echo site.
     */
    static String sizeLog(String process, String sampleId, List<String> shellPaths, String outFile) {
        if (!shellPaths) {
            throw new IllegalArgumentException(
                "ProcessEnvelope.sizeLog: shellPaths must not be empty (${process})")
        }
        // Single-quoted Groovy fragments: `$` has no interpolation meaning here, and a
        // bare `$(`/`${` is exactly what must land in .command.sh. `\\n` inside a
        // single-quoted string is backslash + n -- the two characters bash's `--printf`
        // needs -- not a newline. See probe()'s comment for what over-escaping does.
        def stat = 'size_bytes=$(stat -L --printf="%s\\n" ' + shellPaths.join(' ') +
                   ' 2>/dev/null | awk \'{s+=$1} END {print s+0}\')'
        def cells = [process: process, sample_id: sampleId,
                     filename: filenameCell(shellPaths), bytes: '${size_bytes}']
        return stat + '\n' + row(cells, outFile)
    }

    /**
     * The size-log line for a `stub:` block: `process,sampleId,stub,0`.
     *
     * The process name is the REAL one, not the literal `STUB` the 21 hand-written
     * stub blocks wrote. A stub CSV whose first column says `STUB` cannot be
     * compared with a real one at all -- it groups every process in the run into a
     * single fake row -- and bin/generate_resource_report.py carried a special case
     * to drop it. Same name, zero bytes, is honest and comparable.
     */
    static String sizeLogStub(String process, String sampleId, String outFile) {
        def cells = [process: process, sample_id: sampleId, filename: 'stub', bytes: '0']
        return row(cells, outFile)
    }
}
