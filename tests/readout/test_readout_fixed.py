from __future__ import annotations

import numpy as np
import pytest

from openfly.config import DEFAULT_SETTINGS
from openfly.interfaces import Decision
from openfly.readout import make_readout
from openfly.readout.fixed import FixedDecoder
from tests.experiments.fakes import FakeBrain, make_observation


@pytest.fixture(scope="module")
def brain():
    return FakeBrain(seed=0)


def counts_for(brain, left: int, right: int, gate: int) -> np.ndarray:
    c = np.zeros(brain.n, dtype=np.int32)
    c[brain.populations["DNp20_L"]] = left
    c[brain.populations["DNp20_R"]] = right
    c[brain.populations["DNpe017"]] = gate
    return c


def test_truth_table(brain):
    dec = FixedDecoder(neural_ms=200.0, threshold_hz=2.0)  # 1 spike in 200 ms = 5 Hz
    flat = make_observation(position_lots=0)
    # no gate -> HOLD even with a large difference
    p = dec.predict(counts_for(brain, 0, 3, 0), brain, flat)
    assert p.decision == Decision.HOLD and p.confidence == 0.0 and p.details["side"] == "HOLD"
    # right minus left above 2 Hz with a gate -> ENTER
    p = dec.predict(counts_for(brain, 0, 1, 1), brain, flat)
    assert p.decision == Decision.ENTER and p.details["difference_hz"] == pytest.approx(5.0)
    assert p.realized_over_implied == pytest.approx(0.5)
    assert 0.0 < p.confidence <= 1.0
    # left above right with a gate -> EXIT only while in a position
    in_pos = make_observation(position_lots=-1, entry_credit=200.0)
    p = dec.predict(counts_for(brain, 1, 0, 2), brain, in_pos)
    assert p.decision == Decision.EXIT and p.realized_over_implied == pytest.approx(1.5)
    p = dec.predict(counts_for(brain, 1, 0, 2), brain, flat)
    assert p.decision == Decision.HOLD and p.details["signal"] == "EXIT"
    # ENTER while already in a position -> HOLD
    p = dec.predict(counts_for(brain, 0, 1, 1), brain, in_pos)
    assert p.decision == Decision.HOLD and p.details["signal"] == "ENTER"
    # within the threshold band -> HOLD (difference exactly 0)
    p = dec.predict(counts_for(brain, 1, 1, 1), brain, flat)
    assert p.decision == Decision.HOLD and p.realized_over_implied == pytest.approx(1.0)


def test_bound_columns_and_batch(brain):
    dec = make_readout("fixed", DEFAULT_SETTINGS, brain)
    cols = np.concatenate([brain.populations["DN"], brain.populations["MBON"]])
    cols = np.unique(cols)
    dec.bind_columns(cols)
    full = np.stack([counts_for(brain, 0, 2, 1), counts_for(brain, 2, 0, 1), counts_for(brain, 0, 2, 0)])
    subset = full[:, cols]
    out = dec.predict_batch(subset, brain)
    assert list(out["signal"]) == ["ENTER", "EXIT", "HOLD"]
    single = dec.predict(subset[0], brain, make_observation())
    assert single.decision == Decision.ENTER


def test_config_hash_and_save(tmp_path):
    a = FixedDecoder(neural_ms=200.0)
    b = FixedDecoder(neural_ms=200.0)
    assert a.config_hash() == b.config_hash() and len(a.config_hash()) == 64
    assert FixedDecoder(neural_ms=500.0).config_hash() != a.config_hash()
    a.save(tmp_path)
    loaded = FixedDecoder.load(tmp_path)
    assert loaded.config_hash() == a.config_hash()
