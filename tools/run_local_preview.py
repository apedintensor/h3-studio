"""Start an isolated loopback preview API with an explicit local UI allowlist.

The preview reuses its existing data directory. It never loads production API
settings, starts a GPU controller, or opens the legacy H3 service on port 8844.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from studio_platform.settings import Settings


def preview_settings(data_dir=None, *, api_port=8845, ui_ports=(8850, 8851)):
    if (isinstance(api_port, bool) or not isinstance(api_port, int)
            or not 1024 <= api_port <= 65535 or api_port == 8844):
        raise ValueError("Preview API requires an unprivileged port other than legacy H3 port 8844")
    if (not ui_ports or any(isinstance(port, bool) or not isinstance(port, int)
                           or not 1024 <= port <= 65535 for port in ui_ports)):
        raise ValueError("Preview UI ports must be explicit unprivileged ports")
    origins = tuple(f"http://{host}:{port}" for port in dict.fromkeys(ui_ports)
                    for host in ("127.0.0.1", "localhost"))
    return Settings(data_dir=Path(data_dir) if data_dir else ROOT / ".platform-preview-v2",
                    auth_mode="local-test", generation_enabled=True,
                    execution_backend="mock", render_enabled=True,
                    local_ui_origins=origins)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, help="Existing preview data directory (preserved)")
    parser.add_argument("--port", type=int, default=8845)
    parser.add_argument("--ui-port", action="append", type=int,
                        help="Explicit Vite UI port; may be repeated (defaults: 8850 and 8851)")
    args = parser.parse_args(argv)
    settings = preview_settings(args.data, api_port=args.port,
                                ui_ports=tuple(args.ui_port or (8850, 8851)))
    from studio_platform.api import create_app
    import uvicorn
    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port,
                access_log=False, log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()
