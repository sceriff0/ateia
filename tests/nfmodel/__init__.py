"""One string-aware model of this repo's Nextflow source.

Seven guards used to carry seven private regex parses of the same files, each
with a different blind spot -- so "the guard passed" said nothing about
whether it had read the file. See tests/test_nfmodel.py for the specific
constructs that defeated the old parses.

Guards assert against this model, never against raw text.
"""
from ._lex import block_extent, skip_non_code, strip_comments_and_strings

__all__ = ["block_extent", "skip_non_code", "strip_comments_and_strings"]
