"""run_day through the fake brain, encoder and readout produces a spec-shaped trace."""

from __future__ import annotations

import json
from datetime import time

import pytest

from openfly.execution.brokers import ReplayBroker
from openfly.execution.costs import SimpleCostModel
from openfly.interfaces import REQUIRED_POPULATIONS
from openfly.straddle.replay import DayTrace, run_day
from openfly.worker.stubs import (
    FixedTimeReadout,
    NullBrain,
    NullEncoder,
    aggregate_bars,
    simple_minute_quotes,
)

from .fakes import (
    DAY,
    EXPIRY,
    FakeBrain,
    FakeEncoder,
    FakeReadout,
    make_minute_quotes,
    settings_with,
    synthetic_1m_bars,
    window,
)

STEP_KEYS = {
    "i",
    "t",
    "index",
    "vix",
    "premium",
    "days_to_expiry",
    "stimulus_hash",
    "stimulus_png",
    "rates_hz",
    "fixed_decoder",
    "prediction",
    "guard",
    "action",
    "straddle",
    "fills",
    "pnl_day",
    "compute_seconds",
    "narrative",
    "technical",
}


def run(settings=None, **kwargs):
    settings = settings or settings_with()
    broker = ReplayBroker(SimpleCostModel.from_settings(settings))
    trace = run_day(
        DAY,
        synthetic_1m_bars(),
        make_minute_quotes(),
        kwargs.pop("brain", FakeBrain()),
        kwargs.pop("encoder", FakeEncoder()),
        kwargs.pop("readout", FakeReadout()),
        settings,
        broker,
        window(),
        vix=12.1,
        **kwargs,
    )
    return trace, broker


def test_trace_has_the_spec_fields_and_the_right_actions():
    trace, broker = run()
    assert trace.summary["observations"] == 375
    assert len(trace.steps) >= 375
    for step in trace.steps:
        assert STEP_KEYS <= set(step), set(step) ^ STEP_KEYS
    by_time = {s["t"][11:16]: s for s in trace.steps if s["trigger"] == "observation"}
    first = trace.steps[0]
    assert first["t"] == "2026-09-11T09:16:00+05:30" and first["action"] == "HOLD"
    enter = by_time["10:00"]
    assert enter["action"] == "ENTER"
    assert enter["guard"]["allowed"] is True and len(enter["guard"]["checks"]) == 18
    assert enter["straddle"]["in_position"] is True and enter["straddle"]["strike"] == 23350.0
    assert [f["side"] for f in enter["fills"]] == ["SELL", "SELL"]
    assert enter["prediction"] == {"realized_over_implied": 0.82, "confidence": 0.61, "decision": "ENTER", "tau": 0.1}
    assert set(enter["rates_hz"]) == set(REQUIRED_POPULATIONS)
    neural_s = settings_with()["neural"]["neural_ms"] / 1000.0
    assert enter["fixed_decoder"]["side"] == "ENTER"  # right 3 spikes, left 1 per neuron, gated by DNpe017
    assert enter["fixed_decoder"]["difference_hz"] == pytest.approx(2.0 / neural_s)
    assert enter["stimulus_hash"].startswith("sha256:")
    assert enter["technical"]["encoder"] == "fake" and enter["technical"]["readout"] == "fake"
    assert enter["technical"]["lots"]["lots"] == 1
    assert "10:00. NIFTY 23," in enter["narrative"] and "Sold 1 lot of the 29-SEP-26 monthly straddle at 23350" in enter["narrative"]
    assert enter["expiry"] == "2026-09-29" and enter["expiry_selection"] == "monthly"
    assert enter["straddle"]["expiry"] == "2026-09-29" and enter["straddle"]["expiry_selection"] == "monthly"
    assert trace.config["expiry_selection"] == "monthly"
    assert "Leg stops: call" in enter["narrative"]
    exit_ = by_time["12:00"]
    assert exit_["action"] == "EXIT"
    assert [f["side"] for f in exit_["fills"]] == ["BUY", "BUY"]
    assert exit_["straddle"]["in_position"] is False
    assert exit_["pnl_day"] == pytest.approx(trace.summary["pnl"])
    holding = by_time["11:00"]
    assert holding["action"] == "HOLD" and holding["straddle"]["in_position"] is True
    assert holding["straddle"]["pnl"] > 0  # the premium decays on the calm path
    assert trace.summary["trades"] == 1 and trace.summary["early_exits"] == 1
    assert trace.summary["stop_hits"] == 0 and trace.summary["stop_hits_leg"] == 0 and trace.summary["target_hits"] == 0
    assert trace.summary["pnl"] > 0 and trace.summary["costs"] > 0
    assert broker.positions() == {}
    assert trace.config["interval"] == "1m" and trace.config["encoder_hash"] == "fake-encoder"
    assert all(s["action"] in ("HOLD", "ENTER", "EXIT", "NONE") for s in trace.steps)


def test_json_round_trip_and_save(tmp_path):
    trace, _ = run()
    text = trace.to_json()
    again = DayTrace.from_json(text)
    assert again.steps == trace.steps and again.summary == trace.summary and again.date == "2026-09-11"
    assert json.loads(text)["summary"]["trades"] == 1
    assert len(trace.stimulus_pngs) == 375
    path = trace.save(tmp_path / "rp")
    assert path.name == "trace.json"
    assert (tmp_path / "rp" / "stimulus" / "0.png").read_bytes().startswith(b"\x89PNG")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["steps"][0]["stimulus_png"] == "stimulus/0.png"


def test_five_minute_interval_observes_75_bars():
    settings = settings_with()
    broker = ReplayBroker(SimpleCostModel.from_settings(settings))
    bars = aggregate_bars(synthetic_1m_bars(), 5)
    assert len(bars) == 75
    trace = run_day(DAY, bars, make_minute_quotes(), FakeBrain(), FakeEncoder(False), FakeReadout(), settings, broker, window(), interval="5m", vix=12.1, render_png=False)
    assert trace.summary["observations"] == 75
    assert trace.steps[0]["t"] == "2026-09-11T09:20:00+05:30"
    assert trace.summary["trades"] == 1
    assert trace.stimulus_pngs == {}


def test_reward_pulses_only_in_the_plastic_arm():
    rewards = {}

    def reward_for(index: int) -> float | None:
        rewards[index] = rewards.get(index, 0) + 1
        return 0.5 if index % 2 == 0 else -0.25

    plastic = settings_with(neural__plastic=True, neural__horizon_minutes=60)
    brain = FakeBrain()
    trace, _ = run(plastic, brain=brain, reward_fn=reward_for)
    obs_steps = [s for s in trace.steps if s["trigger"] == "observation"]
    assert obs_steps[59]["technical"]["pulses"] == []
    assert obs_steps[60]["technical"]["pulses"] == [["PAM11", 10.0, 200.0]]
    assert obs_steps[61]["technical"]["pulses"] == [["PPL101", 5.0, 200.0]]
    assert brain.pulses_seen[60] == (("PAM11", 10.0, 200.0),)
    assert min(rewards) == 0 and max(rewards) == 375 - 61
    frozen = FakeBrain()
    trace, _ = run(settings_with(neural__plastic=False), brain=frozen, reward_fn=reward_for)
    assert all(p == () for p in frozen.pulses_seen)


def test_stand_ins_run_the_whole_day():
    settings = settings_with()
    bars = synthetic_1m_bars()
    quotes = simple_minute_quotes(DAY, bars, 12.29, EXPIRY)
    broker = ReplayBroker(SimpleCostModel.from_settings(settings))
    trace = run_day(DAY, bars, quotes, NullBrain(), NullEncoder(), FixedTimeReadout("09:20"), settings, broker, window(), vix=12.29, render_png=False)
    assert trace.summary["observations"] == 375
    assert trace.summary["entries"] == 1
    enter = next(s for s in trace.steps if s["action"] == "ENTER")
    assert enter["t"].endswith("T09:20:00+05:30")
    assert trace.summary["time_exits"] + trace.summary["stop_hits"] + trace.summary["stop_hits_leg"] + trace.summary["target_hits"] == 1
    assert all(v == 0.0 for v in enter["rates_hz"].values())
    assert enter["fixed_decoder"]["side"] == "HOLD"


def test_stop_on_the_minute_path_produces_a_tick_step():
    settings = settings_with()
    broker = ReplayBroker(SimpleCostModel.from_settings(settings))

    def spiky_quotes(when, strike=None):
        q = make_minute_quotes()(when, strike)
        if when.time() >= time(10, 30):
            from openfly.interfaces import Quote, StraddleQuote

            call = Quote(q.call.symbol, "NFO", 140.0, 139.95, 140.05, q.call.timestamp)
            return StraddleQuote(call, q.put, q.strike, q.expiry)
        return q

    trace = run_day(DAY, synthetic_1m_bars(), spiky_quotes, FakeBrain(), FakeEncoder(False), FakeReadout(), settings, broker, window(), vix=12.1, render_png=False)
    leg_stop = next(s for s in trace.steps if s["action"] == "STOP_LEG")
    assert leg_stop["t"].endswith("T10:30:00+05:30") and leg_stop["trigger"] == "broker"
    assert leg_stop["technical"]["observation_i"] == 73  # the 10:30 tick fires before the 10:30 observation (i 74)
    assert leg_stop["fills"][0]["side"] == "BUY" and leg_stop["fills"][0]["symbol"].endswith("CE")
    assert trace.summary["stop_hits_leg"] == 1
    assert trace.summary["trades"] == 1 and trace.summary["early_exits"] == 1  # the put exits on the readout at 12:00
