#!/usr/bin/env python3
"""Check or intentionally regenerate the Markdown API reference."""

from __future__ import annotations

import argparse
from pathlib import Path

from voicey.release.docs import changed_api_reference, write_api_reference


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    changed = changed_api_reference(root)
    if args.check:
        if changed:
            for path in changed:
                print(f"stale API reference: {path}")
            print("Run `uv run python scripts/update_api_reference.py` intentionally.")
            return 1
        print("Generated API reference matches executable public contracts.")
        return 0
    written = write_api_reference(root)
    if written:
        for path in written:
            print(f"updated API reference: {path}")
    else:
        print("Generated API reference was already current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
