"""Persistent local Quick Chat preview, without provider calls or GPU workers."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from studio_platform.settings import Settings


def preview_settings(data_dir=None, frontend_dir=None):
    return Settings(data_dir=data_dir or ROOT / ".platform-quick-chat-preview",
                    frontend_dir=frontend_dir or ROOT.parent / "video-studio-design" / "series",
                    auth_mode="local-test", generation_enabled=False,
                    execution_backend="disabled", render_enabled=False, assistant_enabled=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8870)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--frontend", type=Path)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port in {8844, 8845, 8850, 8851}:
        parser.error("Use a separate unprivileged preview port")
    import uvicorn
    from studio_platform.api import create_app
    app = create_app(preview_settings(args.data, args.frontend))
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False,
                access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
