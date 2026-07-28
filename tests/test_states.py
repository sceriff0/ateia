import numpy as np
from utils.phenotyping.states import state_annotation, state_annotations


def test_three_valued_mapping():
    assert state_annotation("pos") == 1
    assert state_annotation("neg") == -1
    assert state_annotation("free") == 0
    assert state_annotation("contra") == 0


def test_vectorized():
    out = state_annotations(np.array(["pos", "neg", "free", "contra"], dtype=object))
    assert list(out) == [1, -1, 0, 0]
