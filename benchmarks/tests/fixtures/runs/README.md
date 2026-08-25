# Synthetic run fixtures — NOT DATA

`run0000/` and `run0001/` are **hand-written**. They exist so the analysis code
has something deterministic to parse in unit tests, and they must never be
analysed as though they were a benchmark result.

They announce themselves if you look:

* every `submit`/`start`/`complete` timestamp is a literal `-`;
* every input size is an exact power of two;
* every duration is a round number of minutes.

## Why this file exists

These two directories used to sit at `benchmarks/runs/` — the location the
cluster writes real results into — and they were the **only** thing there. A
fresh checkout therefore looked like a benchmark that had been run. It had not:
no sweep has ever been executed, `benchmarks/paper_data/` does not exist, and
`benchmarks/analysis/figures/` holds only its own `.gitignore`.

Moving them under `tests/fixtures/` is what makes the empty result tree
readable as empty. If you ever find run directories at `benchmarks/runs/` in a
fresh checkout again, they are not data either — real runs are written on the
cluster and are gitignored.

## Changing them

`benchmarks/tests/test_load.py`, `test_make_figures.py`, `test_make_tables.py`
and `test_contract.py` all parse these. The numbers are chosen to be obviously
fake, not to be realistic — pick equally obvious ones if you extend them, so
the next reader can tell at a glance what they are looking at.
