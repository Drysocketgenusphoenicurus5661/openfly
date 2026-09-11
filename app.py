"""Run OpenFly with one command:  uv run app.py

Starts the FastAPI backend, serves the built React frontend from
frontend/dist (building it first with pnpm or npm if a Node toolchain is
available and the build is missing), and opens the browser. No .env is
required: every setting, including the OpenAlgo API key, is entered in the
Setup page and stored in data/openfly.db.

Options:
  --host 127.0.0.1   bind address
  --port 8000        port
  --no-browser       do not open a browser tab
  --no-build         never try to build the frontend
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
DIST = FRONTEND / "dist"


def build_frontend() -> bool:
    if not (FRONTEND / "package.json").exists():
        return False
    runner = shutil.which("pnpm") or shutil.which("npm")
    if runner is None:
        print("Node toolchain not found (pnpm or npm). The API will run without the web UI.")
        print("Install Node 20+ and run:  cd frontend && npm install && npm run build")
        return False
    name = Path(runner).stem
    print(f"Building the web UI with {name} (first run only, this can take a minute)...")
    try:
        subprocess.run([runner, "install"], cwd=FRONTEND, check=True)
        subprocess.run([runner, "run", "build"], cwd=FRONTEND, check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"Frontend build failed: {exc}")
        return False
    return DIST.exists()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run OpenFly")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT))
    from openfly.config import PATHS, SettingsStore

    PATHS.ensure()
    settings = SettingsStore().get()
    port = args.port or int(settings["ui"]["port"])

    if not DIST.exists() and not args.no_build:
        build_frontend()

    import uvicorn

    from openfly.api.app import create_app

    app = create_app()
    url = f"http://{args.host}:{port}"
    print(f"OpenFly is starting at {url}")
    if not args.no_browser and settings["ui"].get("open_browser", True):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
