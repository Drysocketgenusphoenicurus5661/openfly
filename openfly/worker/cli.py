"""`openfly worker` and `openfly replay-day`."""

from __future__ import annotations

import argparse
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openfly.config import PATHS, SettingsStore, deep_merge
from openfly.execution.brokers import OpenAlgoBroker, ReplayBroker
from openfly.execution.costs import load_cost_model
from openfly.execution.ledger import Ledger
from openfly.worker.loop import LIVE_ENV, LIVE_VALUE, Worker

IST = ZoneInfo("Asia/Kolkata")


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    worker = subparsers.add_parser("worker", help="run the paper or live straddle loop for one trading day")
    worker.add_argument("--mode", choices=("paper", "live"), default="paper")
    worker.add_argument("--lots", type=int, default=None, help="requested lot count (risk sizing may reduce it)")
    worker.add_argument("--run-dir", default=None, help="run directory (default runs/<mode>-<date>)")
    worker.add_argument("--date", default=None, help="trading date YYYY-MM-DD (default today)")
    worker.add_argument("--interval", default=None, help="observation interval (default settings.neural.live_interval)")
    worker.set_defaults(handler=run_worker_command)

    replay = subparsers.add_parser("replay-day", help="replay one day offline through the fly and the replay broker")
    replay.add_argument("--date", required=True, help="trading date YYYY-MM-DD")
    replay.add_argument("--encoder", default=None)
    replay.add_argument("--readout", default=None)
    replay.add_argument("--neural-ms", type=float, default=None)
    replay.add_argument("--lots", type=int, default=None)
    replay.add_argument("--stop-pct", type=float, default=None)
    replay.add_argument("--target-pct", type=float, default=None)
    replay.add_argument("--interval", default="1m", help="observation interval (default 1m)")
    replay.add_argument("--out", default=None, help="output directory (default runs/replays/rp_<date>_<time>)")
    replay.add_argument("--no-png", action="store_true", help="do not render stimulus images")
    replay.add_argument(
        "--stand-ins",
        action="store_true",
        help="use the null brain, null encoder, fixed-time readout and simple pricer instead of the real packages (fast smoke run)",
    )
    replay.set_defaults(handler=run_replay_day_command)


def _settings_with_overrides(args: argparse.Namespace, store: SettingsStore) -> dict:
    settings = store.get()
    strategy: dict = {}
    neural: dict = {}
    if getattr(args, "lots", None) is not None:
        strategy["lots"] = int(args.lots)
    if getattr(args, "stop_pct", None) is not None:
        strategy["stop_pct"] = float(args.stop_pct)
    if getattr(args, "target_pct", None) is not None:
        strategy["target_pct"] = float(args.target_pct)
    if getattr(args, "encoder", None):
        neural["encoder"] = args.encoder
    if getattr(args, "readout", None):
        neural["readout"] = args.readout
    if getattr(args, "neural_ms", None) is not None:
        neural["neural_ms"] = float(args.neural_ms)
    update = {}
    if strategy:
        update["strategy"] = strategy
    if neural:
        update["neural"] = neural
    return deep_merge(settings, update) if update else settings


def run_worker_command(args: argparse.Namespace) -> int:
    from openfly.worker.factories import load_brain, load_encoder, load_readout

    PATHS.ensure()
    store = SettingsStore()
    settings = _settings_with_overrides(args, store)
    mode = args.mode
    if mode == "live" and os.environ.get(LIVE_ENV) != LIVE_VALUE:
        print(f"live mode refused: set {LIVE_ENV}={LIVE_VALUE} to accept real trades")
        return 2
    day = date.fromisoformat(args.date) if args.date else datetime.now(IST).date()
    run_dir = Path(args.run_dir) if args.run_dir else PATHS.runs / f"{mode}-{day.isoformat()}"
    try:
        from openfly.market.chain import ChainResolver
        from openfly.market.client import OpenAlgoClient
        from openfly.market.feed import LtpFeed
        from openfly.market.session import SessionCalendar
    except ImportError as exc:
        print(f"the market package is not available ({exc}); the worker needs OpenAlgoClient, LtpFeed, SessionCalendar and ChainResolver")
        return 2
    try:
        client = OpenAlgoClient.from_settings(store)
    except Exception as exc:
        print(f"OpenAlgo client could not be created: {exc}")
        return 2
    calendar = SessionCalendar(client, settings)
    chain = ChainResolver(client, settings, calendar)
    feed = LtpFeed.from_settings(store)
    brain, brain_note = load_brain(settings)
    encoder, enc_note = load_encoder(settings["neural"].get("encoder", "B"), settings)
    readout, rd_note = load_readout(settings["neural"].get("readout", "reservoir"), settings)
    print(f"brain: {brain_note}\nencoder: {enc_note}\nreadout: {rd_note}\nrun directory: {run_dir}")
    cost_model = load_cost_model(settings)
    ledger = Ledger(run_dir, cost_model)
    broker = OpenAlgoBroker(client, settings, ledger, mode=mode)
    history_bars = None
    try:
        from openfly.market.history import HistoryCache

        cache = HistoryCache(client)
        interval = args.interval or settings["neural"].get("live_interval", "5m")

        def history_bars():
            from openfly.worker.factories import frame_to_bars

            frame = cache.load(settings["strategy"]["index_exchange"], settings["strategy"]["underlying"], interval)
            return frame_to_bars(frame)

    except ImportError:
        pass
    try:
        feed.start()
    except Exception as exc:
        print(f"feed did not start: {exc}")
    worker = Worker(
        settings,
        mode,
        run_dir,
        brain,
        encoder,
        readout,
        client,
        feed,
        calendar,
        chain,
        broker,
        ledger=ledger,
        cost_model=cost_model,
        history_bars=history_bars,
        trading_date=day,
        interval=args.interval,
    )
    try:
        return worker.run()
    finally:
        try:
            feed.stop()
        except Exception:
            pass
        client.close()


def run_replay_day_command(args: argparse.Namespace) -> int:
    from openfly.straddle.replay import run_day
    from openfly.worker.factories import (
        load_bars,
        load_brain,
        load_encoder,
        load_is_trading_day,
        load_minute_quotes,
        load_readout,
        load_session_window,
        load_vix,
        select_replay_expiry,
    )
    from openfly.worker.stubs import FixedTimeReadout, NullBrain, NullEncoder, simple_minute_quotes

    PATHS.ensure()
    store = SettingsStore()
    settings = _settings_with_overrides(args, store)
    day = date.fromisoformat(args.date)
    window, is_trading, window_note = load_session_window(day, settings)
    print(f"session: {window_note}", flush=True)
    if not is_trading:
        print(f"{day.isoformat()} is not a trading day ({window_note})")
        return 1
    bars, bars_note = load_bars(day, settings, "1m")
    print(f"bars: {bars_note} ({len(bars)} bars)", flush=True)
    vix, vix_note = load_vix(day, settings)
    print(f"vix: {vix:.2f} ({vix_note})", flush=True)
    expiry, expiry_note = select_replay_expiry(day, settings)
    print(f"expiry: {expiry.isoformat()} ({settings['strategy'].get('expiry_selection', 'monthly')}, {expiry_note})", flush=True)
    is_trading_day = load_is_trading_day(settings)
    if args.stand_ins:
        quotes, quotes_note = simple_minute_quotes(day, bars, vix, expiry), "simple_minute_quotes (stand-in)"
        brain, brain_note = NullBrain(), "NullBrain (stand-in)"
        encoder, enc_note = NullEncoder(), "NullEncoder (stand-in)"
        readout, rd_note = FixedTimeReadout.from_settings(settings), "FixedTimeReadout (stand-in)"
        print(f"quotes: {quotes_note}\nbrain: {brain_note}\nencoder: {enc_note}\nreadout: {rd_note}", flush=True)
    else:
        quotes, quotes_note = load_minute_quotes(day, bars, vix, settings, expiry=expiry)
        print(f"quotes: {quotes_note}", flush=True)
        print("loading the brain (the real connectome takes a while; use --stand-ins for a quick run)", flush=True)
        brain, brain_note = load_brain(settings)
        print(f"brain: {brain_note}", flush=True)
        encoder, enc_note = load_encoder(settings["neural"].get("encoder", "B"), settings)
        print(f"encoder: {enc_note}", flush=True)
        readout, rd_note = load_readout(settings["neural"].get("readout", "reservoir"), settings)
        print(f"readout: {rd_note}", flush=True)
    cost_model = load_cost_model(settings)
    broker = ReplayBroker(cost_model)
    trace = run_day(
        day,
        bars,
        quotes,
        brain,
        encoder,
        readout,
        settings,
        broker,
        window,
        interval=args.interval,
        vix=vix,
        render_png=not args.no_png,
        expiry=expiry,
        is_trading_day=is_trading_day,
    )
    trace.config.update({"bars": bars_note, "quotes": quotes_note, "brain": brain_note, "session": window_note, "expiry_source": expiry_note})
    out = Path(args.out) if args.out else PATHS.replays / f"rp_{day.strftime('%Y%m%d')}_{datetime.now(IST).strftime('%H%M%S')}"
    path = trace.save(out)
    s = trace.summary
    print(f"trace written to {path}")
    print(
        f"observations {s['observations']}, steps {s['steps']}, trades {s['trades']}, "
        f"stop hits {s['stop_hits']}, leg stop hits {s['stop_hits_leg']}, target hits {s['target_hits']}, "
        f"time exits {s['time_exits']}, early exits {s['early_exits']}, net P&L INR {s['pnl']:,.0f}"
    )
    for step in trace.steps:
        if step["action"] not in ("NONE", "HOLD"):
            print(f"  {step['t'][11:19]} {step['action']:<10} {step['narrative']}")
    return 0
