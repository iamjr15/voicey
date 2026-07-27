"""Static provider catalog used by validation, wizard choices, and doctor."""

from __future__ import annotations

from typing import Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from voicekit.config.models import RuntimeName, VoicekitModel

ProviderKind: TypeAlias = Literal["stt", "llm", "tts", "carrier"]
PriceClass: TypeAlias = Literal["low", "medium", "high", "variable"]
LatencyClass: TypeAlias = Literal["low", "medium", "high", "network-dependent"]


class ProviderCatalogEntry(VoicekitModel):
    """One selectable model or carrier and the facts shown by the wizard."""

    id: str
    kind: ProviderKind
    runtimes: frozenset[RuntimeName]
    languages: frozenset[str]
    price_class: PriceClass
    latency_class: LatencyClass
    key_env_vars: tuple[str, ...]
    validation_url: str
    validation_headers: dict[str, str] = Field(default_factory=dict[str, str])
    native_idempotency: bool
    description: str

    model_config = VoicekitModel.model_config | {"frozen": True}

    @field_validator("validation_url")
    @classmethod
    def valid_validation_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            msg = (
                "catalog validation_url must be HTTPS. "
                "Fix: configure the provider's authenticated HTTPS validation endpoint."
            )
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def complete_entry(self) -> ProviderCatalogEntry:
        if not self.runtimes:
            msg = "catalog runtimes is empty. Fix: declare at least one supported runtime."
            raise ValueError(msg)
        if self.kind != "carrier" and not self.languages:
            msg = "model languages is empty. Fix: declare BCP-47 tags or '*'."
            raise ValueError(msg)
        if not self.key_env_vars:
            msg = "catalog key_env_vars is empty. Fix: name each required credential."
            raise ValueError(msg)
        return self

    def supports_language(self, language: str) -> bool:
        """Return whether the entry serves a language or its base tag."""
        base = language.split("-", maxsplit=1)[0]
        return "*" in self.languages or language in self.languages or base in self.languages


class ProviderCatalog:
    """Immutable indexed collection with deterministic alternatives."""

    def __init__(self, entries: tuple[ProviderCatalogEntry, ...]) -> None:
        indexed: dict[tuple[ProviderKind, str], ProviderCatalogEntry] = {}
        for entry in entries:
            key = (entry.kind, entry.id)
            if key in indexed:
                msg = f"duplicate provider catalog entry: {entry.kind}/{entry.id}"
                raise AssertionError(msg)
            indexed[key] = entry
        self._entries = entries
        self._indexed = indexed

    @property
    def entries(self) -> tuple[ProviderCatalogEntry, ...]:
        return self._entries

    def get(self, kind: ProviderKind, identifier: str) -> ProviderCatalogEntry | None:
        return self._indexed.get((kind, identifier))

    def alternatives(
        self,
        kind: ProviderKind,
        runtime: RuntimeName,
        language: str | None = None,
    ) -> tuple[str, ...]:
        return tuple(
            sorted(
                entry.id
                for entry in self._entries
                if entry.kind == kind
                and runtime in entry.runtimes
                and (language is None or entry.supports_language(language))
            )
        )


def _entry(
    *,
    id: str,
    kind: ProviderKind,
    languages: frozenset[str],
    price: PriceClass,
    latency: LatencyClass,
    keys: tuple[str, ...],
    url: str,
    headers: dict[str, str],
    native_idempotency: bool = False,
    description: str,
) -> ProviderCatalogEntry:
    return ProviderCatalogEntry(
        id=id,
        kind=kind,
        runtimes=frozenset({"pipecat", "livekit"}),
        languages=languages,
        price_class=price,
        latency_class=latency,
        key_env_vars=keys,
        validation_url=url,
        validation_headers=headers,
        native_idempotency=native_idempotency,
        description=description,
    )


DEFAULT_PROVIDER_CATALOG = ProviderCatalog(
    (
        _entry(
            id="deepgram/nova-3",
            kind="stt",
            languages=frozenset({"*"}),
            price="medium",
            latency="low",
            keys=("DEEPGRAM_API_KEY",),
            url="https://api.deepgram.com/v1/auth/token",
            headers={"Authorization": "Token ${DEEPGRAM_API_KEY}"},
            description="Streaming STT; broad language coverage; usage-priced.",
        ),
        _entry(
            id="openai/gpt-4o-transcribe",
            kind="stt",
            languages=frozenset({"*"}),
            price="medium",
            latency="medium",
            keys=("OPENAI_API_KEY",),
            url="https://api.openai.com/v1/models",
            headers={"Authorization": "Bearer ${OPENAI_API_KEY}"},
            description="Cloud transcription; broad language coverage; usage-priced.",
        ),
        _entry(
            id="anthropic/claude-sonnet-5",
            kind="llm",
            languages=frozenset({"*"}),
            price="high",
            latency="medium",
            keys=("ANTHROPIC_API_KEY",),
            url="https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": "${ANTHROPIC_API_KEY}",
                "anthropic-version": "2023-06-01",
            },
            description="General-purpose Claude model; usage-priced.",
        ),
        _entry(
            id="openai/gpt-5",
            kind="llm",
            languages=frozenset({"*"}),
            price="high",
            latency="medium",
            keys=("OPENAI_API_KEY",),
            url="https://api.openai.com/v1/models",
            headers={"Authorization": "Bearer ${OPENAI_API_KEY}"},
            description="General-purpose OpenAI model; usage-priced.",
        ),
        _entry(
            id="google/gemini-2.5-flash",
            kind="llm",
            languages=frozenset({"*"}),
            price="low",
            latency="low",
            keys=("GEMINI_API_KEY",),
            url="https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": "${GEMINI_API_KEY}"},
            description="Low-latency Gemini model; usage-priced.",
        ),
        _entry(
            id="cartesia/sonic-3.5",
            kind="tts",
            languages=frozenset({"*"}),
            price="medium",
            latency="low",
            keys=("CARTESIA_API_KEY",),
            url="https://api.cartesia.ai/voices?limit=1",
            headers={
                "X-API-Key": "${CARTESIA_API_KEY}",
                "Cartesia-Version": "2025-04-16",
            },
            description="Streaming TTS; low latency; usage-priced.",
        ),
        _entry(
            id="elevenlabs/flash-2.5",
            kind="tts",
            languages=frozenset({"*"}),
            price="medium",
            latency="low",
            keys=("ELEVENLABS_API_KEY",),
            url="https://api.elevenlabs.io/v1/user",
            headers={"xi-api-key": "${ELEVENLABS_API_KEY}"},
            description="Low-latency multilingual TTS; usage-priced.",
        ),
        _entry(
            id="openai/gpt-4o-mini-tts",
            kind="tts",
            languages=frozenset({"*"}),
            price="medium",
            latency="medium",
            keys=("OPENAI_API_KEY",),
            url="https://api.openai.com/v1/models",
            headers={"Authorization": "Bearer ${OPENAI_API_KEY}"},
            description="Instruction-capable cloud TTS; usage-priced.",
        ),
        _entry(
            id="twilio",
            kind="carrier",
            languages=frozenset(),
            price="variable",
            latency="network-dependent",
            keys=("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
            url=("https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}.json"),
            headers={"Authorization": "Basic ${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}"},
            native_idempotency=False,
            description="Programmable voice and SIP carrier; country-specific pricing.",
        ),
        _entry(
            id="telnyx",
            kind="carrier",
            languages=frozenset(),
            price="variable",
            latency="network-dependent",
            keys=("TELNYX_API_KEY",),
            url="https://api.telnyx.com/v2/balance",
            headers={"Authorization": "Bearer ${TELNYX_API_KEY}"},
            native_idempotency=True,
            description="Call Control and SIP carrier; country-specific pricing.",
        ),
    )
)
