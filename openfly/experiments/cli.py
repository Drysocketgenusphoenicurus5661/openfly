"""Command line: experiments and pricer calibration.

    openfly experiment run --encoder B --readout reservoir --neural-ms 200 \
        --train 2025-08-08:2026-03-31 --validation 2026-04-01:2026-06-30 --test 2026-07-01:2026-09-11 \
        [--limit-days 3] [--interval 1m] [--fake-brain] [--plastic] [--horizon 60]
    openfly experiment list
    openfly experiment show <id>
    openfly calibrate-pricer
"""

from __future__ import annotations

import json
import sys

from openfly.config import PATHS, SettingsStore


def real_brain_factory(settings: dict, plastic: bool):
    """Build the connectome Brain lazily; explains what to do when it is missing."""

    def factory():
        try:
            from openfly.neural.brain import Brain
        except ImportError as exc:
            raise RuntimeError(
                "openfly.neural.brain is not available; run with --fake-brain to exercise the harness"
            ) from exc
        hs = float(settings.get("neural", {}).get("half_saturation", 0.5))
        attempts = (
            {"half_saturation": hs, "plastic": plastic},
            {"half_saturation": hs},
            {},
        )
        last: Exception | None = None
        for kwargs in attempts:
            for ctor in (Brain, getattr(Brain, "load", None)):
                if ctor is None:
                    continue
                try:
                    return ctor(**kwargs)
                except TypeError as exc:
                    last = exc
                    continue
                except FileNotFoundError as exc:
                    raise RuntimeError(
                        f"connectome graph missing ({exc}); run the data preparation or use --fake-brain"
                    ) from exc
        raise RuntimeError(f"could not construct Brain: {last}")

    return factory


def fake_brain_factory(seed: int = 0):
    def factory():
        from openfly.experiments.fakebrain import FakeBrain

        return FakeBrain(seed=seed)

    return factory


def _settings() -> dict:
    PATHS.ensure()
    return SettingsStore().get()


def _print_progress(event: dict) -> None:
    stage = event.get("stage")
    done, total = event.get("done", 0), event.get("total", 0)
    if stage == "features" and total:
        if done % 500 == 0 or event.get("cached") or done == total:
            tag = " (cached)" if event.get("cached") else ""
            print(f"features {done}/{total} {event.get('date', '')}{tag}", flush=True)
    elif stage in ("fitting", "evaluating", "targets", "loading", "done"):
        extra = f" {done}/{total}" if total else ""
        print(f"{stage}{extra}", flush=True)


def cmd_experiment_run(args) -> int:
    from openfly.experiments.runner import ExperimentRunner

    settings = _settings()
    config = {
        "encoder": args.encoder,
        "readout": args.readout,
        "plastic": bool(args.plastic),
        "neural_ms": float(args.neural_ms),
        "train": args.train,
        "validation": args.validation,
        "test": args.test,
        "limit_days": args.limit_days,
        "interval": args.interval,
        "horizon_minutes": args.horizon,
        "seed": args.seed,
        "fake_brain": bool(args.fake_brain),
    }
    if args.name:
        config["name"] = args.name
    factory = fake_brain_factory(args.seed) if args.fake_brain else real_brain_factory(settings, bool(args.plastic))
    runner = ExperimentRunner(config, factory, settings, progress=_print_progress)
    print(f"experiment {runner.id} -> {runner.dir}")
    try:
        result = runner.run()
    except Exception as exc:
        print(f"experiment failed: {exc}", file=sys.stderr)
        return 1
    _print_summary(result)
    return 0


def _fmt(value, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _print_summary(result: dict) -> None:
    print(f"id: {result['id']}")
    print(f"name: {result.get('name')}")
    print(f"state: {result.get('state')}")
    obs = result.get("observations", {})
    print(f"observations: {obs.get('total')} ({obs.get('interval')}, horizon {obs.get('horizon_minutes')} min)")
    sel = result.get("selection", {})
    if sel.get("populations"):
        print(f"selected: populations {sel['populations']} alpha {sel['alpha']} tau {sel['tau']} features {sel.get('features')}")
    header = f"{'window':<11}{'net/lot':>10}{'sharpe':>8}{'maxdd':>10}{'trades':>7}{'stop':>5}{'leg':>5}{'tgt':>5}{'acc':>7}{'ci':>16}{'p':>7}{'synth':>7}"
    print(header)
    for w, m in result.get("metrics", {}).items():
        ci = m.get("accuracy_ci")
        ci_txt = f"{ci[0]:.3f}-{ci[1]:.3f}" if ci and ci[0] is not None else "n/a"
        print(
            f"{w:<11}{_fmt(m.get('net_pnl_per_lot'), 0):>10}{_fmt(m.get('sharpe'), 2):>8}{_fmt(m.get('max_drawdown'), 0):>10}"
            f"{m.get('trades', 0):>7}{m.get('stop_hits', 0):>5}{m.get('stop_hits_leg', 0):>5}{m.get('target_hits', 0):>5}"
            f"{_fmt(m.get('accuracy'), 3):>7}{ci_txt:>16}{_fmt(m.get('accuracy_p_value'), 3):>7}{_fmt(m.get('synthetic_fraction'), 2):>7}"
        )
    print("controls (test window):")
    for name, m in result.get("controls", {}).items():
        print(
            f"  {name:<13}net/lot {_fmt(m.get('net_pnl_per_lot'), 0):>8}  sharpe {_fmt(m.get('sharpe'), 2):>6}  "
            f"trades {m.get('trades', 0):>4}  stop {m.get('stop_hits', 0):>3}  leg {m.get('stop_hits_leg', 0):>3}  "
            f"tgt {m.get('target_hits', 0):>3}  acc {_fmt(m.get('accuracy'), 3)}"
        )
    print(f"passed: {result.get('passed')}")
    print(f"verdict: {result.get('verdict')}")


def cmd_experiment_list(args) -> int:
    from openfly.experiments.runner import list_experiments

    rows = list_experiments()
    if not rows:
        print("no experiments")
        return 0
    for r in rows:
        cfg = r.get("config") or {}
        print(
            f"{r['id']}  {r.get('state'):<8} {cfg.get('encoder', '?')}/{cfg.get('readout', '?')}/{cfg.get('interval', '?')}"
            f"  passed={r.get('passed')}  {r.get('verdict') or ''}"
        )
    return 0


def cmd_experiment_show(args) -> int:
    from openfly.experiments.runner import load_experiment

    try:
        result = load_experiment(args.id)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        _print_summary(result)
    return 0


def cmd_calibrate_pricer(args) -> int:
    from openfly.experiments.pricer import calibrate

    _settings()
    try:
        result = calibrate(moneyness_pct=args.moneyness, write=not args.dry_run)
    except Exception as exc:
        print(f"calibration failed: {exc}", file=sys.stderr)
        return 1
    print(f"factor: {result['factor']:.4f}")
    print(f"contracts: {', '.join(result['contracts'])}")
    print(f"rows: {result['rows']} (moneyness within {result['moneyness_pct']} percent)")
    print(f"rmse: {result['rmse_points']:.2f} points, mean abs error {result['mean_abs_pct_error']:.2f} percent")
    for d in result["per_day"]:
        print(
            f"  {d['date']}  rows {d['rows']:>4}  recorded {d['recorded_mean']:>8.2f}  fitted {d['fitted_mean']:>8.2f}"
            f"  dte {d['days_to_expiry_mean']:.2f}"
        )
    if result.get("path"):
        print(f"written: {result['path']}")
    return 0


def register_cli(subparsers) -> None:
    exp = subparsers.add_parser("experiment", help="run, list and show experiments")
    sub = exp.add_subparsers(dest="experiment_command", required=True)

    run = sub.add_parser("run", help="run one experiment")
    run.add_argument("--encoder", default="B", help="A, B or C")
    run.add_argument("--readout", default="reservoir", help="reservoir or fixed")
    run.add_argument("--neural-ms", type=float, default=200.0)
    run.add_argument("--train", required=True, help="start:end")
    run.add_argument("--validation", required=True, help="start:end")
    run.add_argument("--test", required=True, help="start:end")
    run.add_argument("--limit-days", type=int, default=None, help="days per window for smoke runs")
    run.add_argument("--interval", default=None, help="observation interval, 1m (default) or 5m")
    run.add_argument("--horizon", type=int, default=None, help="horizon in minutes (default from settings)")
    run.add_argument("--plastic", action="store_true")
    run.add_argument("--fake-brain", action="store_true", help="use the deterministic FakeBrain")
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--name", default=None)
    run.set_defaults(handler=cmd_experiment_run)

    ls = sub.add_parser("list", help="list experiments")
    ls.set_defaults(handler=cmd_experiment_list)

    show = sub.add_parser("show", help="show one experiment")
    show.add_argument("id")
    show.add_argument("--json", action="store_true")
    show.set_defaults(handler=cmd_experiment_show)

    cal = subparsers.add_parser("calibrate-pricer", help="fit the synthetic straddle IV factor to listed contracts")
    cal.add_argument("--moneyness", type=float, default=1.0, help="max |spot - strike| in percent of spot")
    cal.add_argument("--dry-run", action="store_true", help="do not write calibration.json")
    cal.set_defaults(handler=cmd_calibrate_pricer)
