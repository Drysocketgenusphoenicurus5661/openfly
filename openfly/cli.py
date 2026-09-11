"""OpenFly command line entry point.

Each package registers its own subcommands through a `register_cli(subparsers)`
function so that modules can be developed independently. Missing modules are
skipped, so a partial checkout still exposes the commands it has.
"""

from __future__ import annotations

import argparse
import importlib
import sys

CLI_MODULES = (
    "openfly.connectome.cli",
    "openfly.neural.cli",
    "openfly.market.cli",
    "openfly.experiments.cli",
    "openfly.worker.cli",
    "openfly.api.cli",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openfly", description="OpenFly: fly connectome straddle bot")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in CLI_MODULES:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name and name.startswith(exc.name):
                continue
            raise
        register = getattr(module, "register_cli", None)
        if register is not None:
            register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    result = handler(args)
    return int(result or 0)


if __name__ == "__main__":
    sys.exit(main())
