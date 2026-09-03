"""The *.size.csv header is declared in two languages; they must agree.

`lib/ProcessEnvelope.groovy` owns the column order: `sizeLog` builds each row by
looking its cells up in `SIZE_LOG_COLUMNS`, and `modules/local/aggregate_size_logs.nf`
renders the header with `${ProcessEnvelope.SIZE_LOG_COLUMNS.join(',')}`. But the
reader is Python (`bin/generate_resource_report.py`), and Python cannot import
Groovy. A reorder on one side and not the other would not fail anything: the
report keys on column NAMES via csv.DictReader, so it would silently attribute
each row's bytes to the wrong field and produce a plausible, wrong report.

This test is the seam between the two declarations. It reads the Groovy constant
out of the source text -- comment-stripped, so a commented-out or merely
mentioned constant cannot satisfy it -- and compares it with the Python tuple.
"""

from __future__ import annotations

import importlib.util
import re
import sys

from tests.nfmodel import REPO_ROOT, strip_comments

GROOVY = REPO_ROOT / "lib" / "ProcessEnvelope.groovy"
AGGREGATE = REPO_ROOT / "modules" / "local" / "aggregate_size_logs.nf"

_CONST_RE = re.compile(r"SIZE_LOG_COLUMNS\s*=\s*\[([^\]]*)\]", re.S)


def _groovy_columns():
    src = strip_comments(GROOVY.read_text())
    m = _CONST_RE.search(src)
    assert m, "lib/ProcessEnvelope.groovy declares no SIZE_LOG_COLUMNS list"
    return tuple(re.findall(r"'([^']*)'", m.group(1)))


def _load_report_module():
    spec = importlib.util.spec_from_file_location(
        "grr_schema", REPO_ROOT / "bin" / "generate_resource_report.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["grr_schema"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_python_reader_expects_the_groovy_columns():
    assert _load_report_module().SIZE_LOG_COLUMNS == _groovy_columns()


def test_the_aggregate_header_is_rendered_from_the_constant():
    """AGGREGATE_SIZE_LOGS must not restate the header as a literal.

    Read comment-stripped: this asks "does this line actually run?", and the
    module's own header comment names the columns in prose.
    """
    src = strip_comments(AGGREGATE.read_text())
    assert "ProcessEnvelope.SIZE_LOG_COLUMNS.join(',')" in src, (
        "modules/local/aggregate_size_logs.nf must render its CSV header from "
        "ProcessEnvelope.SIZE_LOG_COLUMNS, not from a literal string"
    )
    assert "process,sample_id,filename,bytes" not in src, (
        "the header is restated as a literal, so a reorder in "
        "lib/ProcessEnvelope.groovy would not reach it"
    )
