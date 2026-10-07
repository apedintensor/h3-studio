"""Isolated loopback Quick Chat preview; generation and assistant calls stay off.

Pass the already-built canonical frontend directory explicitly. This runner
never builds/publishes assets, reads cloud configuration, or starts a GPU worker.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from studio_platform.settings import Settings


def preview_settings(data_dir, frontend_dir):
    return Settings(data_dir=Path(data_dir), frontend_dir=Path(frontend_dir),
                    auth_mode="local-test", generation_enabled=False,
                    execution_backend="disabled", render_enabled=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8870)
    parser.add_argument("--data", type=Path, default=ROOT / ".platform-quick-chat-preview")
    parser.add_argument("--frontend", type=Path, required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port in {8844, 8845, 8850, 8851}:
        parser.error("Use a separate unprivileged preview port")
    import uvicorn
    from studio_platform.api import create_app
    app = create_app(preview_settings(args.data, args.frontend), assistant_enabled=False)
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False,
                access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
