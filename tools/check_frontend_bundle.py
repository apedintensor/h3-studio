"""Validate a frontend artifact locally without extracting or publishing it."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.platform.frontend_bundle import validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        value = validate(args.directory, args.commit)
    except Exception:
        print("Frontend bundle validation failed", file=sys.stderr)
        return 1
    print(json.dumps({"commit": value["commit"], "files": len(value["files"]), "status": "validated"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
