#!/usr/bin/env python3
"""Check or intentionally update the committed public API snapshots."""

from __future__ import annotations

import argparse
from pathlib import Path

from voicey.release.snapshots import changed_snapshots, write_public_snapshots


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Report drift without writing.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    changed = changed_snapshots(root)
    if args.check:
        if changed:
            for path in changed:
                print(f"stale public snapshot: {path}")
            print("Run `uv run python scripts/update_public_snapshots.py` intentionally.")
            return 1
        print("Public API snapshots match their executable sources.")
        return 0
    written = write_public_snapshots(root)
    if written:
        for path in written:
            print(f"updated public snapshot: {path}")
    else:
        print("Public API snapshots were already current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
