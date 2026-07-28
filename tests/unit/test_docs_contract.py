from __future__ import annotations

import re
from pathlib import Path

from voicekit.errors import ERROR_CATALOG

ROOT = Path(__file__).parents[2]
DOCS = ROOT / "docs"

REQUIRED_PAGES = (
    "index.md",
    "concepts.md",
    "quickstart-pipecat.md",
    "quickstart-livekit.md",
    "configuration.md",
    "cli.md",
    "playground.md",
    "tools.md",
    "testing.md",
    "upgrading.md",
    "results-webhooks.md",
    "troubleshooting.md",
    "errors.md",
    "compatibility.md",
    "releasing.md",
    "data-map.md",
    "runtimes/pipecat.md",
    "runtimes/livekit.md",
    "carriers/twilio.md",
    "carriers/telnyx.md",
    "carriers/vobiz.md",
    "carriers/plivo.md",
    "carriers/generic-sip.md",
    "deploy/docker.md",
    "deploy/pipecat-cloud.md",
    "deploy/livekit-cloud.md",
    "deploy/fly-companion.md",
    "deploy/railway.md",
    "api/index.md",
    "api/config.md",
    "api/python.md",
    "api/webhooks.md",
    "api/errors.md",
)
RECIPES = (
    "appointment-booking",
    "front-desk",
    "lead-intake",
    "restaurant-reservations",
)
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
_HTML_LINK = re.compile(r"(?:href|src)=\"([^\"]+)\"")


def test_launch_blocking_docs_inventory_and_quickstarts() -> None:
    for relative in REQUIRED_PAGES:
        page = DOCS / relative
        assert page.is_file(), relative
        assert page.read_text(encoding="utf-8").startswith("#") or page.read_text(
            encoding="utf-8"
        ).startswith("<!-- Generated")

    for runtime in ("pipecat", "livekit"):
        text = (DOCS / f"quickstart-{runtime}.md").read_text(encoding="utf-8")
        assert text.count("<!-- voicekit-doc-test:start -->") == 1
        assert text.count("<!-- voicekit-doc-test:end -->") == 1
        assert f"--runtime {runtime}" in text
        assert "five-minute" in text.lower() or "five minutes" in text.lower()


def test_receiver_docs_have_three_raw_body_verification_examples() -> None:
    text = (DOCS / "results-webhooks.md").read_text(encoding="utf-8")

    for heading in ("### Python", "### JavaScript", "### Go"):
        assert heading in text
    assert "await request.body()" in text
    assert "express.raw" in text
    assert "webhook.Verify(body, r.Header)" in text
    assert "webhook-id" in text
    assert "webhook-timestamp" in text
    assert "webhook-signature" in text


def test_data_map_covers_every_persisted_and_exported_surface() -> None:
    text = (DOCS / "data-map.md").read_text(encoding="utf-8")

    for surface in (
        "Production logs",
        "Debug logs",
        "Call lifecycle",
        "outbox",
        "dead letters",
        "Recordings",
        "Backups",
        "Results webhooks",
        "Prometheus",
        "OTLP traces",
        "Browser session credentials",
    ):
        assert surface in text
    assert "results.purge_after_days" in text
    assert "provider-owned copies" in text


def test_every_recipe_has_demo_audio_and_customization_map() -> None:
    for recipe in RECIPES:
        page = (DOCS / "recipes" / f"{recipe}.md").read_text(encoding="utf-8")
        asset = DOCS / "assets" / "recipes" / f"{recipe}-demo.mp3"

        assert "## Demo audio" in page
        assert "## Production customization map" in page
        assert f"{recipe}-demo.mp3" in page
        assert asset.is_file()
        assert asset.stat().st_size >= 4_096


def test_error_catalog_and_troubleshooting_are_keyed_by_every_code() -> None:
    detailed = (DOCS / "errors.md").read_text(encoding="utf-8")
    generated = (DOCS / "api" / "errors.md").read_text(encoding="utf-8")
    troubleshooting = (DOCS / "troubleshooting.md").read_text(encoding="utf-8")

    for code in ERROR_CATALOG:
        assert f"## {code}" in detailed
        assert f"`{code}`" in generated
    for family in (
        "VK-CFG-*",
        "VK-CLI-*",
        "VK-TEL-*",
        "VK-RUN-*",
        "VK-RES-*",
        "VK-OBS-*",
        "VK-TST-*",
        "VK-DEP-*",
        "VK-REL-*",
        "VK-UPG-*",
        "VK-SEC-*",
    ):
        assert family in troubleshooting


def test_rename_plan_covers_every_public_naming_surface() -> None:
    text = (ROOT / "RENAME.md").read_text(encoding="utf-8")
    for required in (
        "src/voicekit/",
        "project.name",
        "console script",
        "entry-point groups",
        "voicekit.jsonc",
        "environment-variable prefixes",
        "schema snapshots",
        "API reference",
        'rg -i "voicekit"',
        "package",
        "executable",
        "repository",
        "domain",
        "human-only",
    ):
        assert required in text


def test_all_repo_markdown_local_links_resolve() -> None:
    failures: list[str] = []
    pages = [ROOT / "README.md", ROOT / "SECURITY.md", ROOT / "RENAME.md"]
    pages.extend(sorted(DOCS.rglob("*.md")))
    for page in pages:
        text = page.read_text(encoding="utf-8")
        targets = [*_MARKDOWN_LINK.findall(text), *_HTML_LINK.findall(text)]
        for raw_target in targets:
            target = raw_target.strip("<>").split("#", maxsplit=1)[0]
            if (
                not target
                or target.startswith(("http://", "https://", "mailto:", "data:"))
                or "=" in target
            ):
                continue
            resolved = (page.parent / target).resolve()
            if not resolved.exists():
                failures.append(f"{page.relative_to(ROOT)} -> {raw_target}")
    assert failures == []
