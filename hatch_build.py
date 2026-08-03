"""Build the wheel-embedded playground from its pinned npm lockfile."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "playground-web"
OUTPUT = ROOT / "src" / "voicey" / "_frontend"
SKIP_ENV = "VOICEY_SKIP_FRONTEND_BUILD"


class CustomBuildHook(BuildHookInterface):
    """Build hook used for sdists and wheels."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        del version, build_data
        if os.environ.get(SKIP_ENV) == "1":
            self._verify_output(reason=f"{SKIP_ENV}=1")
            return
        npm = shutil.which("npm")
        if npm is None:
            msg = (
                "VY-WEB-005: npm is required to build voicey from source. "
                "Install Node.js 22+, or build from a checkout with bundled assets and "
                f"{SKIP_ENV}=1. See docs/errors.md#vy-web-005."
            )
            raise RuntimeError(msg)
        try:
            subprocess.run([npm, "ci", "--ignore-scripts"], cwd=WEB, check=True)
            subprocess.run([npm, "run", "build"], cwd=WEB, check=True)
        except subprocess.CalledProcessError as exc:
            msg = (
                f"VY-WEB-005: playground build failed with exit {exc.returncode}. "
                "Run `cd playground-web && npm ci && npm run build`. "
                "See docs/errors.md#vy-web-005."
            )
            raise RuntimeError(msg) from exc
        self._verify_output(reason="npm build")

    @staticmethod
    def _verify_output(*, reason: str) -> None:
        if not (OUTPUT / "index.html").is_file():
            msg = (
                f"VY-WEB-005: {reason} did not produce {OUTPUT / 'index.html'}. "
                "Run `cd playground-web && npm ci && npm run build`. "
                "See docs/errors.md#vy-web-005."
            )
            raise RuntimeError(msg)
