/*
========================================================================================
    ProcessEnvelope — the versions.yml boilerplate, rendered once
========================================================================================
    Every process in modules/local/ ends its script: block with a versions.yml heredoc.
    (Most also begin their script: block with a hand-written size-log line — this class
    renders none of that; every module still writes its own.) The versions.yml heredoc
    was written out by hand in every module, and written TWICE per module — once in
    script:, once in stub: with every value replaced by the literal `stub`.

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
     * bin/generate_qc_report.py's parse_versions_yml is a hand-rolled two-level parser
     * keyed on these strings, and the report renders them verbatim in its Process | Tool |
     * Version table. Changing a value here changes a published report.
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
}
