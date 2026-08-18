import numpy as np
import pytest
from benchmarks.analysis.lib.parsing import parse_to_gb, parse_duration


@pytest.mark.parametrize("val,expected", [
    ("1.2 GB", 1.2),
    ("512 MB", 0.5),
    ("1024 KB", 1.0 / 1024),
    ("2 TB", 2048.0),
    ("1073741824 B", 1.0),
    ("0", 0.0),
])
def test_parse_to_gb(val, expected):
    assert parse_to_gb(val) == pytest.approx(expected, rel=1e-6)


def test_parse_to_gb_missing_returns_nan():
    assert np.isnan(parse_to_gb("-"))
    assert np.isnan(parse_to_gb(""))


@pytest.mark.parametrize("val,expected", [
    ("1s", 1.0),
    ("2m 3s", 123.0),
    ("1h 0m 5s", 3605.0),
    ("100ms", 0.1),
    ("1.5s", 1.5),
    ("2m", 120.0),
])
def test_parse_duration(val, expected):
    assert parse_duration(val) == pytest.approx(expected, rel=1e-6)


def test_parse_duration_missing_returns_nan():
    assert np.isnan(parse_duration("-"))
