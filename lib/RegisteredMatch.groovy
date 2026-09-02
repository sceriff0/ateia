/*
========================================================================================
    RegisteredMatch — pair registered output files back to their slide metas
========================================================================================
    A registration backend takes N slides for a patient and returns N registered files.
    Nothing in those filenames identifies which slide each one came from: VALIS renames
    its outputs, and the pipeline deliberately does not parse names to recover identity
    (that was tried; it broke on any patient id containing an underscore). What DOES
    survive registration is the OME channel set, which bin/create_channels_manifest.py
    reads out of each output's OME-XML and writes as filename -> [channel names].

    So the rule is: a registered file belongs to the slide whose samplesheet `channels`
    column names the same SET of markers. That is what `signature` computes and what
    `pair` matches on.

    WHY IT LIVES HERE AND NOT IN THE ADAPTER. It was 84 lines inside a flatMap closure in
    subworkflows/local/adapters/valis_adapter.nf, which made it untestable in isolation
    (a subworkflow's closure has no unit-test surface) and invisible to a second backend
    that needs the same rule. lib/ classes ARE testable, via tests/lib_probe.nf.

    THE FAILURE MODE THIS EXISTS TO PREVENT IS SILENT. Every one of the three throws
    below replaces a case where the wrong meta could be attached to a registered image
    and the run would complete successfully with mislabelled channels all the way out to
    the GeoJSON. There is no "best effort" branch on purpose.

    All methods are static; nothing here reads params.
========================================================================================
*/

class RegisteredMatch {

    /**
     * The signature used for matching: channels lower-cased, sorted, joined with '|'.
     *
     * toSorted(), NEVER sort(). meta.channels is a SHARED List reference across every
     * meta derived with `meta + [k: v]` -- Groovy's Map.plus is cloneSimilarMap(left)
     * followed by putAll(right), i.e. a SHALLOW copy, so the derived map's `channels`
     * value is the same object as the original's. An in-place sort here would reorder
     * that list for every meta holding the reference, and channel ORDER is what
     * --dapi-channel and the pyramid writer index on.
     *
     * Lower-case each element BEFORE sorting, so 'CD3' and 'cd3' sort to the same
     * position. Lower-casing the joined result instead (what the extracted closure did)
     * makes ['CD3','dapi'] and ['cd3','DAPI'] produce different signatures.
     */
    static String signature(List<String> channels) {
        if (channels == null) {
            throw new IllegalArgumentException('RegisteredMatch.signature: channels is null')
        }
        return channels.collect { it.toString().toLowerCase() }.toSorted().join('|')
    }

    /**
     * Pair registered output files back to their slide metas by OME channel signature.
     *
     * `manifest` is the parsed channels-manifest JSON written by
     * bin/create_channels_manifest.py: filename -> [channel names].
     * Returns [[meta, file], ...] in `metas` order.
     *
     * Ordered by metas, not by files, so the emitted order is the samplesheet's rather
     * than a glob's. Both are deterministic; the samplesheet's is the one a reader of
     * the run can predict.
     */
    static List<List> pair(List<Map> metas, List files, Map<String, List<String>> manifest) {
        if (files.size() != metas.size()) {
            throw new IllegalStateException(
                "RegisteredMatch: count mismatch - ${metas.size()} slide(s) but " +
                "${files.size()} registered file(s). " +
                "Slide signatures: ${metas.collect { signature(it.channels as List) }.join(', ')}. " +
                "Files: ${files.collect { it.name }.join(', ')}. " +
                'A registration that returns fewer files than slides is a partial failure; ' +
                'pairing what arrived would publish a short run as a complete one.')
        }

        def metaSignatures = metas.collect { signature(it.channels as List) }
        def duplicates = metaSignatures.countBy { it }.findAll { _sig, n -> n > 1 }.keySet()
        if (duplicates) {
            throw new IllegalStateException(
                "RegisteredMatch: duplicate signature - ${duplicates.join(', ')} " +
                "carried by more than one of the ${metas.size()} slides. Each slide must have a " +
                'unique combination of channels: two slides with the same channel set cannot be ' +
                'told apart from their registered output, and a map keyed on the signature would ' +
                'silently keep only the last of them.')
        }

        def fileBySignature = [:]
        files.each { f ->
            def channels = manifest[f.name]
            if (!channels) {
                throw new IllegalStateException(
                    "RegisteredMatch: unmatched - the channels manifest has no entry for " +
                    "${f.name}. Manifest keys: ${manifest.keySet().join(', ')}. " +
                    'Registered files must carry OME-XML channel names.')
            }
            fileBySignature[signature(channels as List)] = f
        }

        return [metas, metaSignatures].transpose().collect { meta, sig ->
            def f = fileBySignature[sig]
            if (f == null) {
                throw new IllegalStateException(
                    "RegisteredMatch: unmatched - no registered file carries the channel " +
                    "signature ${sig}. Available file signatures: " +
                    "${fileBySignature.keySet().join(', ')}. Check that the samplesheet " +
                    "'channels' column matches the OME-XML channel names.")
            }
            [meta, f]
        }
    }
}
