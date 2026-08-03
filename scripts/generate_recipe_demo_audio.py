#!/usr/bin/env python3
"""Generate or validate the checked-in illustrative recipe demo audio."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import cast

ROOT = Path(__file__).parents[1]
TRANSCRIPTS = ROOT / "docs/assets/recipes/demo-transcripts.json"
ASSET_ROOT = ROOT / "docs/assets/recipes"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate committed assets without requiring the generation tools.",
    )
    args = parser.parse_args()
    transcripts = cast(
        "dict[str, list[dict[str, str]]]",
        json.loads(TRANSCRIPTS.read_text(encoding="utf-8")),
    )
    if args.check:
        failures = [_validate(name) for name in transcripts]
        errors = [failure for failure in failures if failure is not None]
        if errors:
            for error in errors:
                print(f"recipe demo error: {error}")
            return 1
        print(f"Validated {len(transcripts)} checked-in recipe demo audio files.")
        return 0
    say = shutil.which("say")
    ffmpeg = shutil.which("ffmpeg")
    if say is None or ffmpeg is None:
        parser.error("generation requires macOS `say` and `ffmpeg`; use --check in CI")
    for name, turns in transcripts.items():
        _generate(name, turns, say=say, ffmpeg=ffmpeg)
        print(f"generated {ASSET_ROOT / f'{name}-demo.mp3'}")
    return 0


def _generate(
    name: str,
    turns: list[dict[str, str]],
    *,
    say: str,
    ffmpeg: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix=f"voicey-{name}-audio-") as directory:
        temporary = Path(directory)
        segments: list[Path] = []
        for index, turn in enumerate(turns):
            voice = "Samantha" if turn["speaker"] == "caller" else "Alex"
            segment = temporary / f"{index:02d}.aiff"
            subprocess.run(
                [
                    say,
                    "-v",
                    voice,
                    "-o",
                    str(segment),
                    turn["text"],
                ],
                check=True,
                timeout=60,
            )
            segments.append(segment)
        manifest = temporary / "segments.txt"
        manifest.write_text(
            "".join(f"file '{path.as_posix()}'\n" for path in segments),
            encoding="utf-8",
        )
        destination = ASSET_ROOT / f"{name}-demo.mp3"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(manifest),
                "-map_metadata",
                "-1",
                "-ac",
                "1",
                "-ar",
                "22050",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "64k",
                "-y",
                str(destination),
            ],
            check=True,
            timeout=120,
        )


def _validate(name: str) -> str | None:
    path = ASSET_ROOT / f"{name}-demo.mp3"
    if not path.is_file():
        return f"{path} is missing"
    payload = path.read_bytes()
    if len(payload) < 4_096:
        return f"{path} is unexpectedly small"
    if not (payload.startswith(b"ID3") or payload[:2] in {b"\xff\xfb", b"\xff\xf3"}):
        return f"{path} is not an MP3 file"
    return None


if __name__ == "__main__":
    raise SystemExit(main())
