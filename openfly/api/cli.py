"""`openfly api` (API only) and `openfly serve` (API plus the built web UI, opens the browser)."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import threading
import webbrowser
from pathlib import Path

from openfly.config import PATHS, SettingsStore


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    api = subparsers.add_parser("api", help="run the FastAPI backend (no browser, no frontend build)")
    _common(api)
    api.add_argument("--reload", action="store_true", help="auto-reload on code changes (development)")
    api.set_defaults(handler=run_api)

    serve = subparsers.add_parser("serve", help="run the backend, serve the web UI (building it if needed) and open the browser")
    _common(serve)
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument("--no-build", action="store_true", help="never try to build the frontend")
    serve.set_defaults(handler=run_serve)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="default settings.ui.port (8000)")
    parser.add_argument("--log-level", default="info")


def _port(args: argparse.Namespace, settings: dict) -> int:
    return int(args.port or settings.get("ui", {}).get("port", 8000))


def build_frontend(frontend: Path | None = None) -> bool:
    frontend = frontend or PATHS.root / "frontend"
    dist = frontend / "dist"
    if not (frontend / "package.json").exists():
        return False
    runner = shutil.which("pnpm") or shutil.which("npm")
    if runner is None:
        print("Node toolchain not found (pnpm or npm). The API will run without the web UI.")
        print("Install Node 20+ and run:  cd frontend && npm install && npm run build")
        return False
    print(f"Building the web UI with {Path(runner).stem} (first run only, this can take a minute)...")
    try:
        subprocess.run([runner, "install"], cwd=frontend, check=True)
        subprocess.run([runner, "run", "build"], cwd=frontend, check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"Frontend build failed: {exc}")
        return False
    return dist.exists()


def _run(host: str, port: int, log_level: str, reload: bool = False) -> int:
    import uvicorn

    if reload:
        uvicorn.run("openfly.api.app:create_app", factory=True, host=host, port=port, log_level=log_level, reload=True)
        return 0
    from openfly.api.app import create_app

    uvicorn.run(create_app(), host=host, port=port, log_level=log_level)
    return 0


def run_api(args: argparse.Namespace) -> int:
    PATHS.ensure()
    settings = SettingsStore().get()
    port = _port(args, settings)
    print(f"OpenFly API at http://{args.host}:{port}/api/status")
    return _run(args.host, port, args.log_level, reload=bool(getattr(args, "reload", False)))


def run_serve(args: argparse.Namespace) -> int:
    PATHS.ensure()
    settings = SettingsStore().get()
    port = _port(args, settings)
    if not PATHS.frontend_dist.exists() and not args.no_build:
        build_frontend()
    url = f"http://{args.host}:{port}"
    print(f"OpenFly is starting at {url}")
    if not args.no_browser and settings.get("ui", {}).get("open_browser", True):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    return _run(args.host, port, args.log_level)
