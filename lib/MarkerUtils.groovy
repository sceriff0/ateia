/*
 * MarkerUtils - the pipeline's one nuclear/fiducial marker rule, for the Groovy
 * and Nextflow layer.
 *
 * `params.nuclear_markers` (nextflow.config) is the declared source of truth for
 * "which channel is nuclear". Before this class existed, only CONVERT_IMAGE and the
 * Python layer honoured it while three Groovy/NF sites hardcoded the literal 'DAPI':
 * SPLIT_CHANNELS' stub, CsvUtils.validateMetadata, and (implicitly, by unioning every
 * declared channel with no reference awareness) CsvUtils.countChannelsPerPatient.
 * Those three now come through here, and so do SEGMENT's backend guards and the
 * `--nuclear-markers` list SPLIT_CHANNELS hands to bin/split_multichannel.py.
 *
 * THE MARKER LIST IS ALWAYS AN ARGUMENT. This class must never define a default for
 * it: `nextflow.config` is the only place a parameter default may live
 * (tests/test_no_duplicate_param_defaults.py). The Python side already carries the
 * one permitted mirror at bin/utils/metadata.py's DEFAULT_NUCLEAR_MARKERS — do not
 * add a third.
 *
 * Matching is case-insensitive SUBSTRING, deliberately: it mirrors
 * bin/utils/metadata.py's is_nuclear/pick_nuclear_index (which bin/split_multichannel.py
 * now calls instead of testing `"DAPI" in name.upper()` itself), so a channel
 * CONVERT_IMAGE already treated as nuclear (e.g. 'DAPI_nuclear') is treated as nuclear
 * here too.
 */
class MarkerUtils {

    /**
     * The configured markers, trimmed and upper-cased.
     *
     * THIS IS THE ONLY WAY TO READ params.nuclear_markers. Every consumer goes through
     * it (tests/test_nuclear_marker_routing.py enforces that mechanically), because the
     * parameter arrives in three different shapes and two of them break silently:
     *
     *   ['DAPI','CELLTOX']  nextflow.config / a params file      - the intended shape
     *   'CELLTOX'           `--nuclear_markers CELLTOX` on the CLI (nextflow_schema.json
     *                       permits a string; an array cannot be given on a command line)
     *   ['DAPI,CELLTOX']    a params file with the list written as one comma string
     *
     * The bare String, iterated as a List, yields the CHARACTERS C,E,L,L,T,O,X, so
     * 'PANCK' matches on 'C' and is classified nuclear. The one-element comma string is
     * worse: it is a valid Collection, so nothing coerces it, and the single marker
     * 'DAPI,CELLTOX' matches NO channel -- every channel is silently non-nuclear, the
     * nuclear channel is never dropped from moving slides, and the pre-computed group
     * sizes are wrong with no error anywhere. Splitting EVERY element on commas and
     * whitespace closes both.
     *
     * Throws rather than assuming a default, so a missing/empty params.nuclear_markers
     * is loud instead of silently classifying every channel as non-nuclear.
     */
    static List<String> markerList(def nuclearMarkers) {
        def raw
        if (nuclearMarkers == null)                      raw = []
        else if (nuclearMarkers instanceof CharSequence) raw = [nuclearMarkers]
        else if (nuclearMarkers instanceof Object[])     raw = nuclearMarkers as List
        else if (nuclearMarkers instanceof Collection)   raw = nuclearMarkers as List
        else                                             raw = [nuclearMarkers]

        def markers = raw
            .collectMany { it == null ? [] : it.toString().split(/[,\s]+/) as List }
            .collect { it?.trim()?.toUpperCase() }
            .findAll { it }
        if (!markers)
            throw new IllegalArgumentException(
                "No nuclear markers configured. Pass params.nuclear_markers; its default " +
                "lives in nextflow.config and nowhere else.")
        return markers
    }

    /** True when `channel` names one of the configured nuclear/fiducial markers. */
    static boolean isNuclear(def channel, def nuclearMarkers) {
        if (channel == null) return false
        def name = channel.toString().trim().toUpperCase()
        if (!name) return false
        return markerList(nuclearMarkers).any { name.contains(it) }
    }

    /** True when at least one of `channels` is a nuclear/fiducial marker. */
    static boolean hasNuclear(List channels, def nuclearMarkers) {
        return (channels ?: []).any { isNuclear(it, nuclearMarkers) }
    }

    /**
     * Index of the nuclear channel in `channels`, or -1 when there is none.
     *
     * Marker PREFERENCE order wins over channel order, matching
     * bin/utils/metadata.py's pick_nuclear_index: with markers ['DAPI','CELLTOX'] and
     * channels ['CELLTOX','CD8','DAPI'] the answer is 2, not 0. SEGMENT's CellSAM
     * backend feeds this straight to `--dapi-channel`, so a disagreement with the
     * Python resolver would segment the wrong channel rather than fail.
     */
    static int nuclearIndex(List channels, def nuclearMarkers) {
        def names = (channels ?: []).collect { it?.toString()?.trim()?.toUpperCase() ?: '' }
        for (marker in markerList(nuclearMarkers)) {
            def hit = names.findIndexOf { it && it.contains(marker) }
            if (hit >= 0) return hit
        }
        return -1
    }

    /**
     * The nuclear/fiducial channel index a per-slide registration process estimates its
     * transform from — `override` when the operator set one, otherwise resolved from the
     * slide's own channel metadata — or throw naming the slide.
     *
     * TILED_COARSE and TILED_REG_TILE must agree on this index for the same slide: COARSE
     * fits M0 on one channel and REG_TILE measures each tile's residual on it, so a
     * disagreement registers the slide against the wrong marker without failing. They used
     * to carry fifteen byte-identical lines each (resolution + the throw), which is exactly
     * the shape that drifts. `override` is honoured verbatim, including a negative value,
     * which still throws — an explicit out-of-range index is an operator error, not a
     * request to fall back to metadata.
     */
    static int requireNuclearIndex(def override, List channels, def nuclearMarkers,
                                   def processName, def patientId) {
        def index = override != null ? (override as int) : nuclearIndex(channels ?: [], nuclearMarkers)
        if (index < 0)
            throw new IllegalArgumentException(
                "${processName}: no nuclear/fiducial channel among ${channels} for " +
                "patient ${patientId}. Configured nuclear_markers: " +
                "${markerList(nuclearMarkers).join(', ')}. " +
                "Set params.reg_tiled_nuclear_index to override.")
        return index
    }

    /**
     * The channels SPLIT_CHANNELS emits for ONE slide.
     *
     * The nuclear channel is identical across cycles, so it is kept only on the
     * reference slide; keeping it on every moving slide would quantify and merge the
     * same marker once per slide. bin/split_multichannel.py applies the same rule at
     * runtime (`--is-reference`), and SPLIT_CHANNELS' stub and
     * CsvUtils.countChannelsPerPatient now derive theirs from this method, so the
     * group sizes the workflow computes ahead of time match what actually arrives.
     */
    static List splitOutputChannels(List channels, boolean isReference, def nuclearMarkers) {
        if (!channels) return []
        if (isReference) return channels.collect { it }
        return channels.findAll { !isNuclear(it, nuclearMarkers) }
    }
}
