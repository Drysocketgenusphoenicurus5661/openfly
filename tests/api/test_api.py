"""The REST contract of docs/api-spec.md against temp paths and a fake OpenAlgo client."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from openfly.api.services import worker as worker_service
from openfly.api.services.market import choose_expiry
from openfly.config import DEFAULT_SETTINGS

from .conftest import DAY1, DAY2, write_experiment, write_index_bars, write_trace


def test_status_shape(client, fake_client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] and body["python"].startswith("3.")
    assert body["data"]["ready"] is False and body["data"]["graph_path"] is None
    assert body["openalgo"]["reachable"] is True
    assert body["openalgo"]["analyzer_mode"] is True
    assert body["openalgo"]["broker"] == "testbroker"
    assert body["worker"]["state"] == "stopped" and body["worker"]["run_dir"] is None
    assert body["session"]["trading_date"] and body["session"]["trade_start"].endswith("T09:20:00+05:30")
    assert body["session"]["expiry_selection"] == "monthly"
    assert body["chains"] == {"days": 0, "first_date": None, "last_date": None, "error": None, "rows": 0, "coverage": []}
    assert body["live_allowed"] is False
    assert fake_client.closed >= 1


def test_status_when_openalgo_is_unreachable(client, fake_client):
    fake_client.reachable = False
    client.app.state.ctx.market.probe(refresh=True)
    body = client.get("/api/status").json()
    assert body["openalgo"]["reachable"] is False
    assert "ConnectError" in body["openalgo"]["error"]
    assert body["openalgo"]["analyzer_mode"] is False


def test_settings_round_trip_never_returns_the_api_key(client):
    before = client.get("/api/settings").json()
    assert "api_key" not in before["openalgo"]
    assert before["openalgo"]["api_key_set"] is False
    assert before["strategy"]["lots"] == DEFAULT_SETTINGS["strategy"]["lots"]
    assert before["costs"]["stt_sell_pct"] == 0.15

    r = client.put("/api/settings", json={"openalgo": {"api_key": "secret-key"}, "strategy": {"lots": 2}, "costs": {"brokerage_per_order": 15}})
    assert r.status_code == 200
    after = r.json()
    assert "api_key" not in after["openalgo"]
    assert after["openalgo"]["api_key_set"] is True
    assert after["strategy"]["lots"] == 2
    assert after["costs"]["brokerage_per_order"] == 15
    assert after["strategy"]["stop_pct"] == DEFAULT_SETTINGS["strategy"]["stop_pct"]

    again = client.get("/api/settings").json()
    assert again["openalgo"]["api_key_set"] is True and "api_key" not in again["openalgo"]
    assert client.app.state.ctx.store.get()["openalgo"]["api_key"] == "secret-key"

    assert client.put("/api/settings", json={"nonsense": {"a": 1}}).status_code == 400
    assert client.put("/api/settings", json={"strategy": 5}).status_code == 400
    ignored = client.put("/api/settings", json={"openalgo": {"api_key_set": False}}).json()
    assert ignored["openalgo"]["api_key_set"] is True


def test_market_bars_come_from_the_store(client, paths):
    write_index_bars(paths)
    r = client.get("/api/market/bars", params={"symbol": "NIFTY", "exchange": "NSE_INDEX", "interval": "1m", "days": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "NIFTY" and body["exchange"] == "NSE_INDEX" and body["interval"] == "1m"
    assert body["source"] == "BarStore" and body["dates"] == [DAY2.isoformat()]
    assert len(body["bars"]) == 375
    first = body["bars"][0]
    assert first["t"] == "2026-09-11T09:15:00+05:30"
    assert set(first) >= {"t", "o", "h", "l", "c", "v"}
    assert first["o"] == pytest.approx(23350.0)

    two_days = client.get("/api/market/bars", params={"interval": "5m", "days": 2}).json()
    assert two_days["dates"] == [DAY1.isoformat(), DAY2.isoformat()]
    assert len(two_days["bars"]) == 150
    assert two_days["bars"][0]["t"] == "2026-09-10T09:15:00+05:30"
    assert two_days["bars"][0]["h"] == pytest.approx(max(23300.0 + i * 0.1 + 0.5 for i in range(5)))

    assert client.get("/api/market/bars", params={"interval": "7m"}).status_code == 400
    empty = client.get("/api/market/bars", params={"symbol": "BANKNIFTY"}).json()
    assert empty["bars"] == []


def test_session_endpoint_works_offline(client):
    r = client.get("/api/market/session", params={"date": "2026-09-15"})
    assert r.status_code == 200
    body = r.json()
    assert body["trading_date"] == "2026-09-15"
    assert body["is_trading_day"] is True
    assert body["is_expiry_day"] is True  # a Tuesday, weekly rule with no expiry list cached
    assert body["trade_start"] == "2026-09-15T09:20:00+05:30"
    assert body["last_entry"] == "2026-09-15T14:30:00+05:30"
    assert body["square_off"] == "2026-09-15T15:15:00+05:30"
    assert body["now"].endswith("+05:30")
    assert body["expiry_selection"] == "monthly"
    assert client.get("/api/market/session", params={"date": "yesterday"}).status_code == 400
    saturday = client.get("/api/market/session", params={"date": "2026-09-12"}).json()
    assert saturday["is_trading_day"] is False


def test_choose_expiry_rules():
    from datetime import date

    expiries = [date(2026, 9, 15), date(2026, 9, 22), date(2026, 9, 29), date(2026, 10, 6), date(2026, 10, 27)]
    assert choose_expiry(expiries, "weekly", date(2026, 9, 12)) == date(2026, 9, 15)
    assert choose_expiry(expiries, "monthly", date(2026, 9, 12)) == date(2026, 9, 29)
    assert choose_expiry(expiries, "monthly", date(2026, 9, 30)) == date(2026, 10, 27)
    assert choose_expiry(expiries, "weekly", date(2026, 9, 15), min_days_to_expiry=1) == date(2026, 9, 22)
    assert choose_expiry([], "monthly", date(2026, 9, 12)) is None


def test_replay_dates_list_the_stored_days_newest_first(client, paths):
    assert client.get("/api/replay/dates").json()["dates"] == []
    write_index_bars(paths)
    body = client.get("/api/replay/dates").json()
    assert body["dates"] == [DAY2.isoformat(), DAY1.isoformat()]
    assert body["source"].startswith("BarStore NSE_INDEX:NIFTY")


def test_replay_list_get_and_stimulus_from_a_saved_trace(client, paths):
    assert client.get("/api/replay").json() == {"replays": []}
    write_trace(paths, "rp_20260911_120000")
    listing = client.get("/api/replay").json()["replays"]
    assert len(listing) == 1
    item = listing[0]
    assert item["id"] == "rp_20260911_120000" and item["date"] == "2026-09-11" and item["state"] == "done"
    assert item["summary"]["pnl"] == pytest.approx(-125.44) and item["summary"]["trades"] == 1
    assert item["summary"]["premium_source"] == "recorded" and item["summary"]["synthetic_fraction"] == 0.0
    assert item["summary"]["leg_stop_hits"] == 0 and "closed" not in item["summary"]
    assert "steps" not in item

    full = client.get("/api/replay/rp_20260911_120000").json()
    assert len(full["steps"]) == 1
    step = full["steps"][0]
    assert step["stimulus_png"] == "/api/replay/rp_20260911_120000/stimulus/0.png"
    assert step["premium_source"] == "recorded" and step["straddle"]["premium_source"] == "recorded"
    png = client.get(step["stimulus_png"])
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert client.get("/api/replay/rp_20260911_120000/stimulus/7.png").status_code == 404
    assert client.get("/api/replay/nope").status_code == 404
    assert client.get("/api/replay/..").status_code in (404, 405)

    state = client.get("/api/brain/state").json()
    assert state["observed_at"] == "2026-09-11T09:16:00+05:30" and state["source"] == "replay"
    assert state["rates_hz"] == {"KC": 1.2, "DN": 2.1} and state["neural_ms"] == 100.0
    assert client.get("/api/brain/stimulus.png").status_code == 200


def test_brain_endpoints_without_data(client):
    assert client.get("/api/brain/state").status_code == 404
    assert client.get("/api/brain/stimulus.png").status_code == 404
    r = client.get("/api/brain/circuits")
    assert r.status_code == 503 and "prepare" in r.json()["detail"]
    r = client.post("/api/replay/run", json={"date": "2026-09-11"})
    assert r.status_code == 503 and "graph" in r.json()["detail"]


def test_data_status_reports_missing_sources(client):
    body = client.get("/api/data/status").json()
    assert body["stage"] == "missing"
    assert len(body["files"]) == 3 and all(f["present"] is False for f in body["files"])
    assert body["graph"] == {"present": False, "neurons": 0, "edges": 0, "verified": False, "path": "data/graph.npz"}
    assert body["progress"] is None or body["progress"]["stage"]


def test_experiments_list_and_get(client, paths):
    assert client.get("/api/experiments").json() == {"experiments": []}
    write_experiment(paths, "exp_20260912_060339_b_reservoir", state="done")
    write_experiment(paths, "exp_20260912_054931_b_reservoir", state="error")
    listing = client.get("/api/experiments").json()["experiments"]
    assert [e["id"] for e in listing] == ["exp_20260912_060339_b_reservoir", "exp_20260912_054931_b_reservoir"]
    done = listing[0]
    assert done["state"] == "done" and done["progress"] == {"done": 225, "total": 225, "stage": "done"}
    assert done["config"]["encoder"] == "B" and done["name"] == "encoder B, reservoir, frozen"
    assert listing[1]["state"] == "failed"

    one = client.get("/api/experiments/exp_20260912_060339_b_reservoir").json()
    assert one["metrics"]["test"]["trades"] == 1 and one["passed"] is False and one["verdict"] == "smoke run"
    assert client.get("/api/experiments/missing").status_code == 404

    bad = client.post("/api/experiments", json={"encoder": "Z", "readout": "reservoir", "train": ["a", "b"], "validation": ["a", "b"], "test": ["a", "b"]})
    assert bad.status_code == 400
    incomplete = client.post("/api/experiments", json={"encoder": "B", "readout": "reservoir"})
    assert incomplete.status_code == 400 and "train" in incomplete.json()["detail"]


def test_worker_refuses_paper_when_analyzer_is_off(client, fake_client):
    fake_client.analyzer = False
    r = client.post("/api/worker/start", json={"mode": "paper", "lots": 1, "run_dir": None})
    assert r.status_code == 409
    assert "analyzer" in r.json()["detail"].lower()
    assert client.get("/api/status").json()["worker"]["state"] == "stopped"


def test_worker_refuses_when_openalgo_is_unreachable_or_live_is_not_allowed(client, fake_client, paths):
    fake_client.reachable = False
    r = client.post("/api/worker/start", json={"mode": "paper", "lots": 1, "run_dir": None})
    assert r.status_code == 503 and "unreachable" in r.json()["detail"]
    fake_client.reachable = True
    r = client.post("/api/worker/start", json={"mode": "live", "lots": 1, "run_dir": None})
    assert r.status_code == 403 and "OPENFLY_LIVE" in r.json()["detail"]
    assert client.post("/api/worker/start", json={"mode": "sideways"}).status_code == 400
    assert client.post("/api/worker/start", json={"mode": "paper", "lots": 99}).status_code == 400
    assert client.post("/api/worker/squareoff").status_code == 409
    assert client.post("/api/worker/stop").json()["ok"] is True


def test_worker_live_requires_a_passed_experiment(client, fake_client, paths, monkeypatch):
    monkeypatch.setenv("OPENFLY_LIVE", "I_ACCEPT_REAL_TRADES")
    r = client.post("/api/worker/start", json={"mode": "live", "lots": 1, "run_dir": None})
    assert r.status_code == 403 and "passed" in r.json()["detail"]
    write_experiment(paths, "exp_pass", state="done", passed=True)
    r = client.post("/api/worker/start", json={"mode": "live", "lots": 1, "run_dir": None})
    assert r.status_code == 409 and "analyzer" in r.json()["detail"].lower()  # analyzer is on: refuse live


class FakePopen:
    """Stands in for the worker process: alive until the STOP file appears or terminate() is called."""

    instances: list[FakePopen] = []

    def __init__(self, cmd, cwd=None, stdout=None, stderr=None, env=None):
        self.cmd = list(cmd)
        self.run_dir = Path(self.cmd[self.cmd.index("--run-dir") + 1])
        self.pid = 4242
        self.terminated = False
        self.returncode = None
        FakePopen.instances.append(self)

    def poll(self):
        if self.returncode is None and (self.terminated or (self.run_dir / "STOP").exists()):
            self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        deadline = time.monotonic() + (timeout or 5.0)
        while self.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def test_worker_start_stop_and_squareoff_with_a_fake_process(client, paths, monkeypatch):
    monkeypatch.setattr(worker_service.subprocess, "Popen", FakePopen)
    r = client.post("/api/worker/start", json={"mode": "paper", "lots": 1, "run_dir": None})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "starting" and body["run_dir"].startswith("runs/paper-")
    proc = FakePopen.instances[-1]
    assert proc.cmd[-9:-6] == ["worker", "--mode", "paper"] or "worker" in proc.cmd
    assert "--lots" in proc.cmd and proc.cmd[proc.cmd.index("--lots") + 1] == "1"
    status = client.get("/api/status").json()["worker"]
    assert status["state"] == "starting" and status["mode"] == "paper" and status["pid"] == 4242

    again = client.post("/api/worker/start", json={"mode": "paper", "lots": 1, "run_dir": None})
    assert again.status_code == 409

    (proc.run_dir / "state.json").write_text(
        json.dumps({"worker": {"state": "running", "mode": "paper", "run_dir": str(proc.run_dir)}, "straddle": {"in_position": False, "legs": []}, "last_step": None}),
        encoding="utf-8",
    )
    assert client.get("/api/status").json()["worker"]["state"] == "running"
    assert client.get("/api/straddle").json()["in_position"] is False

    so = client.post("/api/worker/squareoff")
    assert so.status_code == 200 and so.json()["ok"] is True and (proc.run_dir / "SQUAREOFF").exists()

    stop = client.post("/api/worker/stop")
    assert stop.status_code == 200 and stop.json()["ok"] is True
    assert (proc.run_dir / "STOP").exists()
    assert proc.poll() == 0
    time.sleep(0.2)
    assert client.get("/api/status").json()["worker"]["state"] in ("stopped", "running")


def test_straddle_intents_orders_positions_degrade_gracefully(client, fake_client):
    flat = client.get("/api/straddle").json()
    assert flat["in_position"] is False and flat["legs"] is None and flat["pnl"] is None
    assert client.get("/api/ledger/intents").json() == {"intents": [], "run_dir": None}
    orders = client.get("/api/orders").json()
    assert [o["orderid"] for o in orders["orders"]] == ["1"] and orders["strategy"] == "openfly"
    positions = client.get("/api/positions").json()
    assert [p["symbol"] for p in positions["positions"]] == ["NIFTY15SEP2623400CE"]
    fake_client.reachable = False
    assert client.get("/api/orders").json()["orders"] == []
    assert "ConnectError" in client.get("/api/orders").json()["error"]
    assert client.get("/api/positions").json()["positions"] == []


def test_analyzer_toggle_calls_the_client(client, fake_client):
    r = client.post("/api/settings/analyzer", json={"mode": False})
    assert r.status_code == 200 and r.json() == {"analyzer_mode": False}
    assert fake_client.toggled == [False]
    assert client.post("/api/settings/analyzer", json={}).status_code == 400
    assert client.get("/api/status").json()["openalgo"]["analyzer_mode"] is False


def test_events_websocket_backlog_and_ping(client, paths):
    write_trace(paths)
    with client.websocket_connect("/api/events") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["data"]["worker"]["state"] == "stopped"
        ws.send_json({"action": "ping"})
        seen = []
        for _ in range(10):
            message = ws.receive_json()
            seen.append(message["type"])
            if message["type"] == "pong":
                break
        assert "pong" in seen
        client.app.state.ctx.bus.publish("log", {"message": "hello from a thread"})
        message = ws.receive_json()
        assert message["type"] == "log" and message["data"]["message"] == "hello from a thread" and message["at"]


def test_spa_fallback_serves_the_built_frontend(client, paths):
    dist = paths.frontend_dist
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>OpenFly</title><div id=root></div>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('fly')", encoding="utf-8")
    root = client.get("/")
    assert root.status_code == 200 and "id=root" in root.text and "text/html" in root.headers["content-type"]
    deep = client.get("/replay/rp_20260911_120000")
    assert deep.status_code == 200 and "id=root" in deep.text
    asset = client.get("/assets/app.js")
    assert asset.status_code == 200 and "fly" in asset.text and "immutable" in asset.headers["cache-control"]
    missing = client.get("/api/nope")
    assert missing.status_code == 404 and missing.json()["detail"].startswith("no API route")
    outside = client.get("/../pyproject.toml")
    assert outside.status_code == 200 and "id=root" in outside.text


def test_placeholder_page_when_the_frontend_is_not_built(client):
    root = client.get("/")
    assert root.status_code == 200 and "pnpm build" in root.text
    assert "OpenFly API is running" in client.get("/dashboard").text


def test_errors_are_json_with_detail(client):
    r = client.get("/api/market/bars", params={"days": 0})
    assert r.status_code == 422 and "detail" in r.json()
    r = client.get("/api/experiments/../x")
    assert r.status_code in (404, 405)
