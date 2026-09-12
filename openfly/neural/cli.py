"""Neural subcommands: benchmark, circuits, observe-test."""

from __future__ import annotations

import argparse
import sys
import time

from openfly.interfaces import REQUIRED_POPULATIONS


def _load_brain(args: argparse.Namespace):
    from openfly.neural.brain import Brain

    t0 = time.perf_counter()
    brain = Brain(
        half_saturation=getattr(args, "half_saturation", 0.5),
        plastic=getattr(args, "plastic", False),
    )
    print(
        f"Loaded {brain.n:,} neurons and {brain.edges:,} edges in {time.perf_counter() - t0:.1f} s",
        flush=True,
    )
    return brain


def cmd_benchmark(args: argparse.Namespace) -> int:
    from openfly.neural.benchmark import benchmark, format_rows, recommend_neural_ms

    brain = _load_brain(args)
    print(
        f"Benchmark: {args.neural_ms:.0f} ms of neural time per stimulus after {args.warmup_ms:.0f} ms warm-up",
        flush=True,
    )
    rows = benchmark(brain, neural_ms=args.neural_ms, warmup_ms=args.warmup_ms)
    print(format_rows(rows))
    rec = recommend_neural_ms(rows)
    print(
        f"Worst case {rec['worst_seconds_per_100ms']:.3f} s per 100 ms; budget {rec['budget_seconds_per_observation']:.2f} s per observation "
        f"for 21,000 observations in 6 hours; max neural_ms {rec['max_neural_ms']:.0f}; recommended {rec['recommended_neural_ms']:.0f} ms per bar"
    )
    return 0


def cmd_circuits(args: argparse.Namespace) -> int:
    brain = _load_brain(args)
    sizes = brain.population_sizes()
    print("Populations:")
    for name in REQUIRED_POPULATIONS:
        print(f"  {name}: {sizes[name]:,}")
    extra = [n for n in sizes if n not in REQUIRED_POPULATIONS]
    for name in extra:
        print(f"  {name}: {sizes[name]:,} (extra)")
    defs = brain.provenance()["population_definitions"]
    print("Definitions:")
    for k, v in defs.items():
        print(f"  {k}: {v}")
    return 0


def cmd_observe_test(args: argparse.Namespace) -> int:
    from openfly.neural.benchmark import uniform_stimulus

    brain = _load_brain(args)
    white = uniform_stimulus(brain, 1.0)
    kc = brain.populations["KC"]
    for i in range(3):
        res = brain.observe(white, args.neural_ms)
        rates = brain.population_rates(res.counts, res.neural_ms)
        print(
            f"observation {i + 1}: white field {res.neural_ms:.0f} ms, {res.compute_seconds:.2f} s wall, "
            f"total spikes {int(res.counts.sum()):,}, KC spikes {int(res.counts[kc].sum()):,} "
            f"({int((res.counts[kc] > 0).sum())} of {len(kc)} cells), KC {rates['KC']:.2f} Hz, "
            f"lamina {rates['lamina']:.1f} Hz, R1-R6 {rates['R1-R6']:.1f} Hz, DN {rates['DN']:.2f} Hz"
        )
    for pop in ("PAM11", "PPL101"):
        idx = brain.populations[pop]
        stim = uniform_stimulus(brain, 1.0, pulses=((pop, 20.0, 200.0),))
        res = brain.observe(stim, max(args.neural_ms, 200.0))
        print(
            f"{pop} pulse 20 mV for 200 ms: {pop} spikes {int(res.counts[idx].sum()):,} "
            f"({int((res.counts[idx] > 0).sum())} of {len(idx)} cells), KC spikes {int(res.counts[kc].sum()):,}, "
            f"{res.compute_seconds:.2f} s wall"
        )
    if brain.plasticity is not None:
        print(f"Plasticity: {brain.plasticity.summary()}")
    return 0


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "benchmark",
        help="wall seconds per 100 ms of neural time for dark, mid-grey and white fields",
    )
    p.add_argument(
        "--neural-ms", type=float, default=500.0, help="neural time per stimulus (default 500)"
    )
    p.add_argument(
        "--warmup-ms", type=float, default=100.0, help="untimed warm-up per stimulus (default 100)"
    )
    p.add_argument("--half-saturation", type=float, default=0.5)
    p.set_defaults(handler=cmd_benchmark)

    p = subparsers.add_parser("circuits", help="print population sizes")
    p.set_defaults(handler=cmd_circuits)

    p = subparsers.add_parser(
        "observe-test", help="three white-field observations and dopamine pulses"
    )
    p.add_argument("--neural-ms", type=float, default=200.0)
    p.add_argument("--half-saturation", type=float, default=0.5)
    p.add_argument("--plastic", action="store_true", help="enable the KC to MBON plasticity arm")
    p.set_defaults(handler=cmd_observe_test)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="openfly-neural")
    register_cli(parser.add_subparsers(dest="command", required=True))
    ns = parser.parse_args()
    sys.exit(ns.handler(ns))
