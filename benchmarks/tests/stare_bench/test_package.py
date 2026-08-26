from benchmarks.stare_bench import __version__


def test_version_is_a_dotted_string():
    assert isinstance(__version__, str)
    assert __version__.count(".") == 2
