"""Prepare a disposable, intentionally broken project for the P1 manual doctor gate."""

from __future__ import annotations

import argparse
from pathlib import Path

from voicekit.config.manifest import ManifestStore, ProjectManifest, RecipeSelection
from voicekit.config.models import ModelAxis


def prepare(root: Path) -> None:
    """Create only safe local breakages; never mutate the host machine."""
    root.mkdir(parents=True, exist_ok=True)
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    ManifestStore(root / "voicekit.jsonc").save(
        ProjectManifest(
            project_name="broken-doctor-fixture",
            runtime="pipecat",
            recipe=RecipeSelection(name="scratch", version="1.0.0"),
            channels=frozenset({"phone", "web"}),
            models=models,
            carriers=["twilio"],
            phone_number="+14155550123",
        )
    )
    (root / ".env.example").write_text(
        "DEEPGRAM_API_KEY=\n"
        "ANTHROPIC_API_KEY=\n"
        "CARTESIA_API_KEY=\n"
        "TWILIO_ACCOUNT_SID=\n"
        "TWILIO_AUTH_TOKEN=\n"
        "VOICEKIT_WEBHOOK_SECRET=\n"
        "INTENTIONALLY_UNDOCUMENTED_KEY=\n",
        encoding="utf-8",
    )
    (root / "agent.py").write_text(
        """from voicekit import Results

results = Results(
    webhook="https://127.0.0.1:1/unreachable",
    secret_env="VOICEKIT_WEBHOOK_SECRET",
)
""",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text("dist/\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    prepare(args.root.resolve())
    print(args.root.resolve())


if __name__ == "__main__":
    main()
