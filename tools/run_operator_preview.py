"""Isolated console preview. Inventory GETs are opt-in; no starts or generation."""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frontend', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8897)
    parser.add_argument('--data', type=Path, default=ROOT/'.platform-operator-preview')
    parser.add_argument('--scan-inventory', nargs='+', choices=('lium','targon'),
        help='Explicitly enable read-only supplier stock sampling; never enables rental or generation')
    args = parser.parse_args()
    if not 1024<=args.port<=65535 or args.port in {8844,8845,8850,8851,8870}:
        parser.error('Use a separate loopback preview port')
    from studio_platform.api import create_app
    from studio_platform.settings import Settings
    from studio_platform.operator_capacity import OperatorRegistry
    from studio_platform.runtime_catalog import public_catalog
    import uvicorn
    settings = Settings(data_dir=args.data.resolve(),frontend_dir=args.frontend.resolve(),
        auth_mode='local-test',generation_enabled=False,execution_backend='disabled',render_enabled=False,
        operator_capacity_owners=('superdan','supervan'))
    app = create_app(settings,operator_registry=OperatorRegistry(catalog=public_catalog))
    scanner = None
    if args.scan_inventory:
        from studio_platform.capacity_scan import MarketScanner
        scanner = MarketScanner(app.state.repository, providers=args.scan_inventory)
        scanner.start()
    try:
        uvicorn.run(app,host='127.0.0.1',port=args.port,proxy_headers=False,access_log=False,log_level='warning')
    finally:
        if scanner:
            scanner.stop()


if __name__=='__main__':
    main()
