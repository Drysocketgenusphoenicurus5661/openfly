"""Connectome subcommands: prepare, verify, graph-info."""

from __future__ import annotations

import argparse
import sys
import time

from openfly.config import PATHS
from openfly.connectome import download as dl
from openfly.connectome import verify as vf
from openfly.connectome.compile import compile_graph, read_manifest
from openfly.connectome.sources import SOURCES


class _ProgressPrinter:
    """Prints download progress at most every percent or every 2 seconds."""

    def __init__(self, name: str):
        self.name = name
        self.last_pct = -1
        self.last_time = 0.0

    def __call__(self, done: int, total: int) -> None:
        pct = int(100 * done / total) if total else 100
        now = time.monotonic()
        if pct != self.last_pct and (now - self.last_time > 2.0 or pct >= 100):
            self.last_pct = pct
            self.last_time = now
            print(f"  {self.name}: {done / 1e6:,.1f} of {total / 1e6:,.1f} MB ({pct}%)", flush=True)


def cmd_prepare(args: argparse.Namespace) -> int:
    PATHS.ensure()
    print(f"Connectome sources in {PATHS.malecns}")
    for src in SOURCES:
        if dl.is_verified(src):
            print(f"  {src.name}: present and verified")
            continue
        print(f"  {src.name}: downloading from {src.url}")
        try:
            dl.download_source(src, progress=_ProgressPrinter(src.name))
        except dl.DownloadError as exc:
            print(f"  download failed: {exc}")
            return 1
        print(f"  {src.name}: downloaded and verified")

    graph_ok = vf.verify_graph()["ok"]
    if graph_ok and not args.force:
        print(f"Compiled graph {PATHS.graph} is present and verified (use --force to recompile)")
    else:
        if args.force and PATHS.graph.exists():
            print("Recompiling (--force)")
        print("Compiling graph")
        t0 = time.perf_counter()
        manifest = compile_graph(progress=lambda m: print(m, flush=True))
        seconds = time.perf_counter() - t0
        peak = manifest["memory"]["peak_working_set_bytes"]
        peak_text = f"{peak / 1e9:.2f} GB" if peak else "unknown"
        print(f"Compile wall time {seconds:.1f} s, peak working set {peak_text}")

    report = vf.verify()
    print(vf.format_report(report))
    return 0 if report["ok"] else 1


def cmd_verify(args: argparse.Namespace) -> int:
    report = vf.verify()
    print(vf.format_report(report))
    return 0 if report["ok"] else 1


def cmd_graph_info(args: argparse.Namespace) -> int:
    manifest = read_manifest()
    lock = vf.read_lock()
    if manifest is None or lock is None:
        print(f"No compiled graph at {PATHS.graph} (run: openfly prepare)")
        return 1
    c = manifest["counts"]
    print(f"Graph: {PATHS.graph}")
    print(f"Dataset: {manifest['dataset']} ({manifest['license']})")
    print(f"Neurons {c['neurons']:,}, edges {c['edges']:,}, contacts {c['contacts']:,}")
    print(f"Self edges {c['self_edges']:,}, weight-1 edges {c['weight_one_edges']:,}")
    nt = manifest["neurotransmitters"]
    print(
        f"Sign +1 {nt['sign_plus']:,}, sign -1 {nt['sign_minus']:,}, uncertain {nt['uncertain']:,}, modulatory {nt['modulatory']:,}"
    )
    ph = manifest["photoreceptors"]
    print(
        f"R1-R6 mapped {ph['r16_mapped']} of {ph['r16_total']} (left {ph['r16_left']}, right {ph['r16_right']})"
    )
    print(f"R8 mapped {ph['r8_mapped']} (R8p {ph['r8p_mapped']}, R8y {ph['r8y_mapped']})")
    print("Type counts:")
    for k, v in manifest["type_counts"].items():
        print(f"  {k}: {v}")
    print("Arrays:")
    for name, info in lock["arrays"].items():
        print(f"  {name}: {info['dtype']} {tuple(info['shape'])} sha256 {info['sha256'][:16]}")
    t = manifest["timing"]
    peak = manifest["memory"]["peak_working_set_bytes"]
    print(
        f"Compiled in {t['total_s']} s, peak working set {peak / 1e9:.2f} GB"
        if peak
        else f"Compiled in {t['total_s']} s"
    )
    print("Caveats:")
    for line in manifest["caveats"]:
        print(f"  {line}")
    return 0


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "prepare", help="download (if missing), verify, normalize and compile the connectome"
    )
    p.add_argument(
        "--force", action="store_true", help="recompile even if graph.npz is present and verified"
    )
    p.set_defaults(handler=cmd_prepare)

    p = subparsers.add_parser("verify", help="verify source hashes and compiled array hashes")
    p.set_defaults(handler=cmd_verify)

    p = subparsers.add_parser(
        "graph-info", help="print counts, arrays and caveats of the compiled graph"
    )
    p.set_defaults(handler=cmd_graph_info)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="openfly-connectome")
    register_cli(parser.add_subparsers(dest="command", required=True))
    ns = parser.parse_args()
    sys.exit(ns.handler(ns))
