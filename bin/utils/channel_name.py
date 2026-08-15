"""A marker's DECLARED name vs its FILE STEM — the Python half of one rule.

    DECLARED   the samplesheet's spelling (``HLA.DR``). It is the marker's
               IDENTITY and the string that fills the ``<marker>`` slot of the
               ``"<marker>: <Compartment>: <Statistic>"`` key that
               ``qupath-extension-flowpath`` parses case-sensitively.
    FILE STEM  the sanitised, filesystem-safe form (``HLA_DR``). It names files
               and nothing else.

``lib/ChannelName.groovy`` is the same rule for the Groovy/Nextflow layer, and it
is the one the PIPELINE calls: ``SPLIT_CHANNELS`` computes the stems there and
hands them to ``bin/split_multichannel.py`` as ``--file-stems``, which is what
makes that process's ``script:`` and ``stub:`` paths name a channel identically
instead of relying on two sanitisers agreeing.

This module is therefore the fallback, not the pipeline path: standalone
invocation, and the case where channel names are read from OME-XML rather than
passed in. ``tests/test_channel_identity.py``'s ``SANITISER_TABLE`` is the shared
table both halves are held to; the Groovy half asserts the same rows in
``tests/lib_probe.nf``.

The allowlist is ASCII (``[A-Za-z0-9-_]``) and deliberately narrower than the
``str.isalnum()`` it replaces: ``isalnum()`` is Unicode-aware, so a marker spelled
with a Greek beta kept the beta in its filename on the Python side and could not
be reproduced on the Groovy side without shipping a Unicode table there. A rule
two languages must agree on cannot depend on a Unicode category.

Import convention: flat (``from channel_name import file_stems``) by scripts that
``sys.path.insert(0, .../bin/utils)`` first, matching ``measurements`` and
``metadata``. Pure stdlib.
"""

from __future__ import annotations

from typing import List, Sequence

__all__ = ["ALLOWED", "file_stem", "file_stems"]

# Everything outside this set becomes '_'.
ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


def file_stem(declared: str) -> str:
    """The filesystem-safe stem for ONE declared name.

    Not unique on its own — ``CD3.105`` and ``CD3_105`` both give ``CD3_105``.
    Use :func:`file_stems` for a list; it disambiguates.
    """
    return "".join(c if c in ALLOWED else "_" for c in str(declared or ""))


def file_stems(declared: Sequence[str]) -> List[str]:
    """Stems for a whole declared list: index-aligned, and unique.

    Numbering is by POSITION IN THE DECLARED LIST (``_2``, ``_3``, …), not by
    what is already on disk. Disk-order numbering — an ``os.path.exists`` probe,
    which is what this replaced — gave a different answer depending on which
    channels were actually written, so a reference slide (nuclear channel kept)
    and a moving slide (nuclear channel dropped) could number the same collision
    differently, and the Nextflow stub, which writes a different set of files
    again, differently a third time. Position numbering is a pure function of the
    samplesheet.
    """
    taken = set()
    out: List[str] = []
    for name in declared or []:
        stem = file_stem(name)
        if stem in taken:
            suffix = 2
            while f"{stem}_{suffix}" in taken:
                suffix += 1
            stem = f"{stem}_{suffix}"
        taken.add(stem)
        out.append(stem)
    return out
