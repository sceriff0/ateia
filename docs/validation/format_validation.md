# Format validation

This page is the evidence RULING R3 trades for the vendor fixtures it forbids
committing: the formats named in the table below cannot be synthesised, so they
are validated only against real files — on a cluster with access to them, using
`tests/cluster/validate_formats.sh` (see `tests/cluster/README.md` in the
repository).

## Synthesised formats — covered on every push

Everything the fixture generator *can* produce is exercised by
`tests/integration/formats/`, in CI's `format-tests` job, on real bytes, on
every push: pyramidal OME-TIFF, BigTIFF, interleaved RGB, 8-bit, float32,
HDF5, a single Hamamatsu-shaped slide plus its multi-file manifest, and the
seven end-to-end `CONVERT_IMAGE` conversions and the truncated-input refusal.
No cluster run is needed for any of these, and none of them appear in the table
below — that table exists only for the formats CI cannot generate.

## Vendor formats — kit-validated: pending

No cluster run has been recorded yet.

- pipeline commit: pending
- probe container: pending

| format | status |
|---|---|
| `.czi` | kit-validated: pending — see `tests/cluster/README.md` |
| `.nd2` | kit-validated: pending — see `tests/cluster/README.md` |
| `.lif` | kit-validated: pending — see `tests/cluster/README.md` |
| `.ndpi` | kit-validated: pending — see `tests/cluster/README.md` |
| `.svs` | kit-validated: pending — see `tests/cluster/README.md` |

`tests/cluster/README.md` has the three commands an operator with access to
real vendor slides needs to run. The first of them,
`tests/cluster/validate_formats.sh`, produces this page's replacement: a
report carrying the pipeline's 40-character commit SHA, the probe container
digest, and one row per format actually probed — copied over this file and
committed in its place.

`tests/test_validation_report_is_real.py` accepts this pending state as
legal — it is an explicit, honest "not yet run", never a synonym for a missing
file — but requires a *real* report, once one lands, to be a real measurement:
it must name the commit it was produced from and the container the probe ran
in, carry a row for every one of the five formats above, never be the
template with its placeholder paths still in it, and record at least one
successful conversion. It deliberately does not require every row to be `OK`:
a recorded failure is a finding about the readers, not a reason to delete the
row.
