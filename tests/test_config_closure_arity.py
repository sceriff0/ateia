"""A `path` input's arity is not fixed, and conf/*.config closures forgot it.

Nextflow binds a `path` input to a bare `Path` when the group holds exactly one
file and to a `List<Path>` when it holds more. `MERGE_AND_PYRAMID`'s memory
closure called `split_channels.collect { it.size() }` directly, so on a
one-channel patient it iterated the Path's NAME ELEMENTS and went looking for a
file called "channels" -- the stageAs directory. The run aborted with
"No such file or directory: channels" (reproduced 2026-08-25 by
tests/modules/merge_and_pyramid.nf.test's one-channel case).

The rule this pins is deliberately blunt: inside a conf/modules.config closure,
never iterate a name you did not bind yourself. Bind a local first -- which is
where the `instanceof Collection` normalisation has to go anyway -- and iterate
that. Blunt because the closure has no way to ask how many files it got: it is
config text, evaluated per task, with no access to the process declaration that
would say. A guard that tried to distinguish "input that can be plural" from
"input that cannot" would need exactly the cross-file knowledge that is
unavailable here, and would go quiet the first time it guessed wrong.

Why a static guard at all, when an nf-test now covers the one real site: the
nf-test covers THAT site. Nothing stops the next memory closure from being
written the same way, and the failure only appears for a group size no other
test in the file uses -- which is precisely how this one survived.
"""
import re

from tests.nfmodel import with_name_blocks

# Receiver of a `.collect {` / `.each {` / `.any {` ... iteration.
_ITER = re.compile(r"\b([a-z_]\w*)\s*\.\s*(collect|each|find|findAll|any|every|sum|inject)\s*\{")
# Names bound inside the closure itself.
_LOCAL = re.compile(r"\bdef\s+([a-z_]\w*)")

# Bound by Nextflow in a process-scope closure and safe to iterate: none of
# these is a staged path whose arity varies.
SAFE_RECEIVERS = {"task", "params", "workflow", "meta", "it"}


def _closure_bodies():
    """Every `= { ... }` directive body in conf/modules.config, on the
    comment/string-stripped view so prose and quoted text cannot match."""
    for block in with_name_blocks():
        for m in re.finditer(r"=\s*\{", block.body):
            start = m.end()
            depth, i = 1, start
            while i < len(block.body) and depth:
                depth += (block.body[i] == "{") - (block.body[i] == "}")
                i += 1
            yield block.selector, block.start_line, block.body[start:i - 1]


def test_no_closure_iterates_an_unbound_name():
    offenders = []
    for selector, line, body in _closure_bodies():
        locals_ = set(_LOCAL.findall(body)) | SAFE_RECEIVERS
        for m in _ITER.finditer(body):
            receiver = m.group(1)
            if receiver not in locals_:
                offenders.append(
                    f"conf/modules.config:{line} withName: '{selector}' "
                    f"iterates `{receiver}.{m.group(2)}{{}}` without binding it "
                    f"first -- if `{receiver}` is a path input, Nextflow hands it "
                    f"a bare Path for a one-file group and the iteration walks "
                    f"the path's name elements instead of the files"
                )
    assert not offenders, "\n".join(offenders) + (
        "\n\nBind a local first and normalise while you are there:\n"
        "    def files = (x instanceof Collection) ? x : [x]"
    )


def test_the_scan_finds_iterations_to_check():
    """MERGE_AND_PYRAMID's memory closure iterates. If this finds nothing, the
    extractor is stale and the guard above is passing vacuously."""
    found = [
        (sel, line, m.group(0))
        for sel, line, body in _closure_bodies()
        for m in _ITER.finditer(body)
    ]
    assert found, "no closure iterations found in conf/modules.config"


def test_the_one_file_normalisation_is_still_present():
    """The positive half. If the `instanceof Collection` line is deleted, the
    guard above still passes -- `files` would simply be bound to something
    else -- so the normalisation itself has to be asserted, not just the shape
    of the code around it."""
    bodies = [b for _s, _l, b in _closure_bodies() if "split_channels" in b]
    assert bodies, "MERGE_AND_PYRAMID's memory closure no longer names split_channels"
    assert any("instanceof Collection" in b for b in bodies), (
        "MERGE_AND_PYRAMID's memory closure no longer normalises split_channels "
        "to a list. A one-channel patient aborts the run without it."
    )
