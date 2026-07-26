from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from voicekit import Agent, Behavior, Limits, Models, Phone, Results, Voice, Web
from voicekit.config import (
    DEFAULT_PROVIDER_CATALOG,
    ManifestState,
    ManifestStore,
    ProjectManifest,
    ProviderCatalog,
    ProviderCatalogEntry,
    RecipeSelection,
    collect_config_issues,
    validate_agent_config,
)
from voicekit.config.validation import ConfigValidationError
from voicekit.errors import VoicekitError
from voicekit.results.signing import encode_secret


def example_tool(query: str) -> dict[str, str]:
    """Return a deterministic result."""
    return {"query": query}


def _agent(**changes: Any) -> Agent:
    values: dict[str, Any] = {
        "name": "clinic-front-desk",
        "runtime": "pipecat",
        "models": Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
            fallbacks={"tts": "elevenlabs/flash-2.5"},
        ),
        "voice": Voice(language="en"),
        "persona": "Warm, brisk, and professional.",
        "flow": "flow:entry",
        "tools": "tools",
        "phone": None,
        "web": Web(enabled=True, allowed_origins=["https://example.test"]),
        "results": Results(
            webhook="https://receiver.example.test/results",
            secret_env="VOICEKIT_WEBHOOK_SECRET",  # pragma: allowlist secret
        ),
    }
    values.update(changes)
    return Agent(**values)


def _valid_environment() -> dict[str, str]:
    return {
        "ANTHROPIC_API_KEY": "anthropic-test",  # pragma: allowlist secret
        "CARTESIA_API_KEY": "cartesia-test",  # pragma: allowlist secret
        "DEEPGRAM_API_KEY": "deepgram-test",  # pragma: allowlist secret
        "ELEVENLABS_API_KEY": "elevenlabs-test",  # pragma: allowlist secret
        "VOICEKIT_WEBHOOK_SECRET": encode_secret(b"current-test-key"),
    }


def _manifest() -> ProjectManifest:
    return ProjectManifest(
        project_name="clinic",
        runtime="pipecat",
        recipe=RecipeSelection(name="appointment-booking", version="1.0.0"),
        channels=frozenset({"web"}),
        models={
            "stt": "deepgram/nova-3",
            "llm": "anthropic/claude-sonnet-5",
            "tts": "cartesia/sonic-3.5",
        },
        state=ManifestState(completed_steps=["runtime"], last_command="voicekit init"),
    )


def test_agent_wire_form_and_hash_are_deterministic() -> None:
    first = _agent()
    wire_form = first.model_dump(mode="json")
    reordered = Agent.model_validate(dict(reversed(tuple(wire_form.items()))))

    assert first.config_hash == reordered.config_hash
    assert first.config_hash.startswith("sha256:")
    assert len(first.config_hash) == len("sha256:") + 64
    assert first.config_hash != _agent(persona="Different instructions.").config_hash
    assert "current-test-key" not in json.dumps(first.model_dump(mode="json"))


def test_agent_serializes_importable_callable_tools() -> None:
    agent = _agent(tools=[example_tool])

    assert agent.model_dump(mode="json")["tools"] == [f"{example_tool.__module__}:example_tool"]


def test_agent_rejects_local_callable_tools_with_a_fix() -> None:
    def local_tool() -> None:
        return None

    with pytest.raises(ValidationError, match="Fix: move the function"):
        _agent(tools=[local_tool])


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (lambda: _agent(name="Not Valid"), "Fix: use at most 63"),
        (lambda: Models(stt="bad", llm="a/b", tts="c/d"), "Fix: use a catalog id"),
        (
            lambda: Models(
                stt="a/b",
                llm="c/d",
                tts="e/f",
                fallbacks={"tts": "e/f"},
            ),
            "Fix: choose a different fallback",
        ),
        (lambda: Voice(language="english"), "Fix: use a tag"),
        (lambda: Voice(speed=2.1), "Fix: choose a value"),
        (
            lambda: Phone(provider="twilio", number="4155550123"),
            "Fix: use '+' followed",
        ),
        (
            lambda: Phone(
                provider="twilio",
                number="+14155550123",
                inbound=False,
                outbound=False,
            ),
            "Fix: enable at least one",
        ),
        (
            lambda: Web(enabled=True, allowed_origins=[]),
            "Fix: list every browser origin",
        ),
        (
            lambda: Web(enabled=True, allowed_origins=["https://x.test/path"]),
            "Fix: use scheme + host",
        ),
        (
            lambda: Results(
                webhook="http://receiver.test",
                secret_env="VOICEKIT_WEBHOOK_SECRET",  # pragma: allowlist secret
            ),
            "Fix: use an https://",
        ),
        (
            lambda: Results(
                webhook="https://receiver.test",
                secret_env="not-valid",  # pragma: allowlist secret
            ),
            "Fix: use uppercase",
        ),
        (
            lambda: Limits(max_duration_s=10, silence_hangup_s=10),
            "Fix: lower silence",
        ),
        (
            lambda: Behavior(transfer_number="1234"),
            "Fix: use '+' followed",
        ),
        (
            lambda: _agent(web=Web(), phone=None),
            "Fix: configure phone",
        ),
    ],
)
def test_structural_validation_errors_carry_fixes(
    factory: Callable[[], object],
    expected: str,
) -> None:
    with pytest.raises(ValidationError, match=expected.replace("+", r"\+")):
        factory()


def test_web_origins_and_phrase_lists_are_normalized() -> None:
    web = Web(
        enabled=True,
        allowed_origins=["https://example.test/", "https://example.test"],
    )
    behavior = Behavior(end_call_phrases=[" GOODBYE ", "goodbye", "Bye Now"])

    assert web.allowed_origins == ["https://example.test"]
    assert behavior.end_call_phrases == ["goodbye", "bye now"]


def test_catalog_exposes_runtime_language_auth_and_idempotency_facts() -> None:
    deepgram = DEFAULT_PROVIDER_CATALOG.get("stt", "deepgram/nova-3")
    twilio = DEFAULT_PROVIDER_CATALOG.get("carrier", "twilio")
    telnyx = DEFAULT_PROVIDER_CATALOG.get("carrier", "telnyx")

    assert deepgram is not None
    assert deepgram.supports_language("en-US")
    assert deepgram.validation_headers == {"Authorization": "Token ${DEEPGRAM_API_KEY}"}
    assert twilio is not None
    assert not twilio.native_idempotency
    assert telnyx is not None
    assert telnyx.native_idempotency
    assert DEFAULT_PROVIDER_CATALOG.alternatives("tts", "pipecat", "en") == (
        "cartesia/sonic-3.5",
        "elevenlabs/flash-2.5",
        "openai/gpt-4o-mini-tts",
    )


def test_catalog_rejects_duplicate_entries_as_an_invariant() -> None:
    entry = DEFAULT_PROVIDER_CATALOG.entries[0]

    with pytest.raises(AssertionError, match="duplicate provider catalog"):
        ProviderCatalog((entry, entry))


def test_catalog_validation_collects_all_missing_keys() -> None:
    issues = collect_config_issues(_agent(), environ={})

    assert {issue.code for issue in issues} == {"VK-CFG-105"}
    assert {issue.path for issue in issues} == {
        "env.ANTHROPIC_API_KEY",
        "env.CARTESIA_API_KEY",
        "env.DEEPGRAM_API_KEY",
        "env.ELEVENLABS_API_KEY",
        "env.VOICEKIT_WEBHOOK_SECRET",
    }
    assert all("voicekit keys add" in issue.fix for issue in issues)


def test_catalog_validation_reports_unknown_models_carriers_and_bad_secret() -> None:
    agent = _agent(
        models=Models(
            stt="unknown/stt",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        phone=Phone(provider="vobiz", number="+14155550123"),
    )
    environment = _valid_environment() | {"VOICEKIT_WEBHOOK_SECRET": "not-a-secret"}

    issues = collect_config_issues(agent, environ=environment)

    assert {issue.code for issue in issues} == {
        "VK-CFG-101",
        "VK-CFG-104",
        "VK-CFG-106",
    }
    assert all(issue.fix for issue in issues)


def test_catalog_validation_checks_runtime_and_language() -> None:
    constrained = ProviderCatalog(
        (
            ProviderCatalogEntry(
                id="only/stt",
                kind="stt",
                runtimes=frozenset({"livekit"}),
                languages=frozenset({"fr"}),
                price_class="low",
                latency_class="low",
                key_env_vars=("ONLY_STT_KEY",),
                validation_url="https://only.example.test/key",
                native_idempotency=False,
                description="Test-only constrained STT.",
            ),
            *tuple(
                entry for entry in DEFAULT_PROVIDER_CATALOG.entries if entry.kind in {"llm", "tts"}
            ),
        )
    )
    agent = _agent(
        models=Models(
            stt="only/stt",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        )
    )

    issues = collect_config_issues(agent, environ=_valid_environment(), catalog=constrained)

    assert {"VK-CFG-102", "VK-CFG-103"} <= {issue.code for issue in issues}


def test_validated_agent_is_returned_and_aggregate_error_is_cataloged() -> None:
    agent = _agent()

    assert validate_agent_config(agent, environ=_valid_environment()) is agent
    with pytest.raises(ConfigValidationError) as caught:
        validate_agent_config(agent, environ={})

    assert caught.value.code == "VK-CFG-001"
    assert caught.value.issues
    assert "Fix:" in str(caught.value)


def test_manifest_loads_json5_and_round_trips_atomically(tmp_path: Path) -> None:
    path = tmp_path / "voicekit.jsonc"
    path.write_text(
        """
        {
          // JSON5 comments and trailing commas are supported.
          project_name: "clinic",
          runtime: "pipecat",
          recipe: {name: "appointment-booking", version: "1.0.0"},
          channels: ["web"],
          models: {
            stt: "deepgram/nova-3",
            llm: "anthropic/claude-sonnet-5",
            tts: "cartesia/sonic-3.5",
          },
        }
        """,
        encoding="utf-8",
    )
    store = ManifestStore(path)

    loaded = store.load()
    store.save(loaded)

    assert store.load() == loaded
    assert path.read_text(encoding="utf-8").startswith("// Managed by voicekit.")
    assert "SECRET" not in path.read_text(encoding="utf-8")
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o644


def test_manifest_has_public_json_wire_schema() -> None:
    payload = _manifest().model_dump(mode="json")

    assert payload["schema_version"] == 1
    assert payload["channels"] == ["web"]
    assert payload["state"]["completed_steps"] == ["runtime"]
    assert "secret" not in json.dumps(payload).casefold()


def test_manifest_read_and_write_failures_are_cataloged(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.jsonc"
    invalid.write_text("{runtime: 'not-a-runtime'}", encoding="utf-8")

    with pytest.raises(VoicekitError) as read_error:
        ManifestStore(invalid).load()
    assert read_error.value.code == "VK-CFG-002"
    assert "Fix:" in str(read_error.value)

    parent_is_file = tmp_path / "file"
    parent_is_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(VoicekitError) as write_error:
        ManifestStore(parent_is_file / "voicekit.jsonc").save(_manifest())
    assert write_error.value.code == "VK-CFG-003"
    assert "Fix:" in str(write_error.value)
