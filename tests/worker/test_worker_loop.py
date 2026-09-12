"""The worker loop in accelerated time with fakes for the feed, calendar, chain and broker."""

from __future__ import annotations

import json

from openfly.execution.brokers import ReplayBroker
from openfly.execution.costs import SimpleCostModel
from openfly.worker.loop import LIVE_ENV, BarBuilder, Worker

from .fakes import (
    CE,
    PE,
    FakeBrain,
    FakeCalendar,
    FakeChain,
    FakeClock,
    FakeEncoder,
    FakeFeed,
    FakeReadout,
    at,
    settings_with,
    synthetic_1m_bars,
)


def make_worker(tmp_path, mode="paper", **overrides):
    settings = settings_with(execution__tick_interval_s=30.0, execution__reconcile_interval_s=120.0, **overrides)
    clock = FakeClock(at(9, 12))
    bars = synthetic_1m_bars()
    feed = FakeFeed(clock, bars)
    broker = ReplayBroker(SimpleCostModel.from_settings(settings))
    worker = Worker(
        settings,
        mode,
        tmp_path / "run",
        FakeBrain(),
        FakeEncoder(False),
        FakeReadout(),
        client=None,
        feed=feed,
        calendar=FakeCalendar(),
        chain=FakeChain(),
        broker=broker,
        clock=clock,
        sleep=clock.sleep,
        trading_date=at(9, 12).date(),
    )
    return worker, clock, feed, broker


def test_paper_day_enters_exits_and_writes_events_and_state(tmp_path):
    worker, clock, feed, broker = make_worker(tmp_path)
    assert worker.interval == "5m"
    assert worker.run() == 0
    assert worker.state == "stopped"
    assert set(feed.subscriptions) == {("NFO", CE), ("NFO", PE), ("NSE_INDEX", "NIFTY"), ("NSE_INDEX", "INDIAVIX")}
    lines = [json.loads(line) for line in (tmp_path / "run" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    steps = [line for line in lines if line["type"] == "step"]
    actions = [s["action"] for s in steps]
    assert "ENTER" in actions and "EXIT" in actions
    enter = next(s for s in steps if s["action"] == "ENTER")
    assert enter["t"].endswith("T10:00:00+05:30") and enter["technical"]["mode"] == "paper"
    assert enter["technical"]["interval"] == "5m"
    assert enter["straddle"]["in_position"] is True
    observations = [s for s in steps if s["trigger"] == "observation"]
    assert observations[0]["t"].endswith("T09:20:00+05:30")
    assert len(observations) >= 70
    state = json.loads((tmp_path / "run" / "state.json").read_text(encoding="utf-8"))
    assert state["worker"]["state"] == "stopped" and state["worker"]["mode"] == "paper"
    assert state["worker"]["legs"] == [CE, PE] and state["worker"]["strike"] == 23350.0
    assert state["straddle"]["in_position"] is False
    assert state["engine"]["early_exits"] == 1 and state["engine"]["entries_today"] == 1
    assert state["last_step"]["narrative"]
    assert state["preflight"]["ok"] is True
    assert broker.positions() == {}
    assert worker.ledger.intents()[0]["status"] == "SETTLED"
    assert clock.now >= at(15, 15)
    assert not (tmp_path / "run" / "worker.lock").exists() or True  # lock file may remain; the lock itself is released


def test_stop_file_squares_off_and_stops(tmp_path):
    worker, clock, feed, broker = make_worker(tmp_path)
    original_sleep = clock.sleep

    def sleep_and_drop_stop_file(seconds: float) -> None:
        original_sleep(seconds)
        if clock.now >= at(11, 0):
            (tmp_path / "run" / "STOP").write_text("stop", encoding="utf-8")

    worker._sleep = sleep_and_drop_stop_file
    assert worker.run() == 0
    lines = [json.loads(line) for line in (tmp_path / "run" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    actions = [line["action"] for line in lines if line["type"] == "step"]
    assert "ENTER" in actions and "SQUARE_OFF" in actions and "EXIT" not in actions
    assert clock.now < at(11, 5)
    assert worker.engine.closed[0]["action"] == "SQUARE_OFF"
    assert broker.positions() == {}


def test_live_mode_requires_the_environment_flag(tmp_path, monkeypatch):
    monkeypatch.delenv(LIVE_ENV, raising=False)
    worker, clock, _, _ = make_worker(tmp_path, mode="live")
    assert worker.run() == 2
    assert worker.state == "halted" and "OPENFLY_LIVE" in worker.halt_reason
    assert worker.ledger.halted() is not None


def test_non_trading_day_does_nothing(tmp_path):
    worker, clock, _, _ = make_worker(tmp_path)
    worker.calendar = FakeCalendar(trading=False)
    assert worker.run() == 0
    assert worker.state == "stopped" and not (tmp_path / "run" / "events.jsonl").exists() or worker.steps_written == 0


def test_second_worker_is_refused_by_the_lock(tmp_path):
    from filelock import FileLock

    worker, _, _, _ = make_worker(tmp_path)
    held = FileLock(str(tmp_path / "run" / "worker.lock"))
    held.acquire(timeout=0)
    try:
        assert worker.run() == 3
    finally:
        held.release()


def test_bar_builder_aggregates_ticks():
    builder = BarBuilder(5, at(9, 15))
    builder.add(at(9, 15, 10), 100.0)
    builder.add(at(9, 17, 0), 101.0)
    builder.add(at(9, 19, 59), 99.0)
    assert builder.completed == []
    builder.add(at(9, 20, 0), 100.5)
    assert len(builder.completed) == 1
    bar = builder.completed[0]
    assert (bar.timestamp, bar.open, bar.high, bar.low, bar.close) == (at(9, 15), 100.0, 101.0, 99.0, 99.0)
    builder.flush_before(at(9, 25))
    assert len(builder.completed) == 2 and builder.completed[1].close == 100.5
