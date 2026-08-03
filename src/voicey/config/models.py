"""Canonical Pydantic configuration models and deterministic wire form."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Callable
from typing import Any, Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

RuntimeName: TypeAlias = Literal["pipecat", "livekit"]
ModelAxis: TypeAlias = Literal["stt", "llm", "tts"]
PhoneProvider: TypeAlias = Literal["twilio", "telnyx", "vobiz", "plivo", "sip"]
VoicemailBehavior: TypeAlias = Literal["hangup", "leave_message"]
ResultField: TypeAlias = Literal["transcript", "data", "recording", "metrics"]
ToolCallable: TypeAlias = Callable[..., Any]
ToolReference: TypeAlias = str | ToolCallable

MODEL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
IMPORT_REFERENCE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$")
E164_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")
METRICS_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._~/-]*$")


class VoiceyModel(BaseModel):
    """Strict base model shared by config and manifest schemas."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class Models(VoiceyModel):
    """STT, LLM, and TTS model selections plus axis-specific failover."""

    stt: str
    llm: str
    tts: str
    fallbacks: dict[ModelAxis, str] = Field(default_factory=dict[ModelAxis, str])

    @field_validator("stt", "llm", "tts")
    @classmethod
    def valid_model_id(cls, value: str) -> str:
        return _validate_model_id(value)

    @field_validator("fallbacks")
    @classmethod
    def valid_fallback_ids(cls, value: dict[ModelAxis, str]) -> dict[ModelAxis, str]:
        return {axis: _validate_model_id(model_id) for axis, model_id in value.items()}

    @model_validator(mode="after")
    def fallbacks_differ_from_primary_models(self) -> Models:
        for axis, fallback in self.fallbacks.items():
            if fallback == getattr(self, axis):
                msg = (
                    f"models.fallbacks.{axis} duplicates models.{axis}. "
                    "Fix: choose a different fallback or remove it."
                )
                raise ValueError(msg)
        return self


class Voice(VoiceyModel):
    """Voice identity and language configuration."""

    id: str | None = None
    language: str = "en"
    fallback_language: str | None = None
    speed: float = 1.0

    @field_validator("language", "fallback_language")
    @classmethod
    def valid_language(cls, value: str | None) -> str | None:
        if value is not None and not LANGUAGE_PATTERN.fullmatch(value):
            msg = f"{value!r} is not a BCP-47 language tag. Fix: use a tag such as 'en' or 'en-US'."
            raise ValueError(msg)
        return value

    @field_validator("speed")
    @classmethod
    def valid_speed(cls, value: float) -> float:
        if not 0.5 <= value <= 2.0:
            msg = "voice.speed is outside 0.5-2.0. Fix: choose a value from 0.5 through 2.0."
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def distinct_fallback_language(self) -> Voice:
        if self.fallback_language == self.language:
            msg = "fallback_language duplicates language. Fix: remove it or choose another tag."
            raise ValueError(msg)
        return self


class Phone(VoiceyModel):
    """Phone-channel configuration."""

    provider: PhoneProvider
    number: str
    inbound: bool = True
    outbound: bool = True
    record: bool = False

    @field_validator("number")
    @classmethod
    def valid_e164(cls, value: str) -> str:
        return _validate_e164(value, field_name="phone.number")

    @model_validator(mode="after")
    def has_direction(self) -> Phone:
        if not self.inbound and not self.outbound:
            msg = (
                "phone.inbound and phone.outbound are both false. "
                "Fix: enable at least one direction or remove phone."
            )
            raise ValueError(msg)
        return self


class Web(VoiceyModel):
    """Browser-channel configuration."""

    enabled: bool = False
    allowed_origins: list[str] = Field(default_factory=list)

    @field_validator("allowed_origins")
    @classmethod
    def valid_origins(cls, values: list[str]) -> list[str]:
        unique: list[str] = []
        for origin in values:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                msg = (
                    f"{origin!r} is not an HTTP(S) origin. "
                    "Fix: use scheme + host + optional port with no path or credentials."
                )
                raise ValueError(msg)
            normalized = origin.removesuffix("/")
            if normalized not in unique:
                unique.append(normalized)
        return unique

    @model_validator(mode="after")
    def origins_required_when_enabled(self) -> Web:
        if self.enabled and not self.allowed_origins:
            msg = (
                "web.enabled is true but allowed_origins is empty. "
                "Fix: list every browser origin that may create a session."
            )
            raise ValueError(msg)
        return self


class Results(VoiceyModel):
    """Result delivery, rotation, redaction, and retention configuration."""

    webhook: str
    secret_env: str
    previous_secret_env: str | None = None
    include: list[ResultField] = Field(
        default_factory=lambda: ["transcript", "data", "recording", "metrics"]
    )
    redact: list[str] = Field(default_factory=list)
    purge_after_days: int = 30

    @field_validator("webhook", mode="before")
    @classmethod
    def webhook_is_https(cls, value: Any) -> Any:
        parsed = urlsplit(str(value))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            msg = f"{value} is not HTTPS. Fix: use an https:// results receiver."
            raise ValueError(msg)
        return value

    @field_validator("secret_env", "previous_secret_env")
    @classmethod
    def valid_env_name(cls, value: str | None) -> str | None:
        if value is not None and not ENV_NAME_PATTERN.fullmatch(value):
            msg = (
                f"{value!r} is not an environment-variable name. "
                "Fix: use uppercase letters, digits, and underscores."
            )
            raise ValueError(msg)
        return value

    @field_validator("include")
    @classmethod
    def nonempty_unique_include(cls, values: list[ResultField]) -> list[ResultField]:
        if not values:
            msg = "results.include is empty. Fix: include at least 'data'."
            raise ValueError(msg)
        return list(dict.fromkeys(values))

    @field_validator("redact")
    @classmethod
    def valid_redaction_paths(cls, values: list[str]) -> list[str]:
        if any(not path or path.startswith(".") or path.endswith(".") for path in values):
            msg = (
                "results.redact contains an empty or malformed field path. "
                "Fix: use names such as 'phone_number' or 'data.email'."
            )
            raise ValueError(msg)
        return list(dict.fromkeys(values))

    @field_validator("purge_after_days")
    @classmethod
    def valid_retention(cls, value: int) -> int:
        if not 1 <= value <= 3650:
            msg = (
                "results.purge_after_days is outside 1-3650. "
                "Fix: choose a retention period from 1 through 3650 days."
            )
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def secrets_are_distinct(self) -> Results:
        if self.previous_secret_env == self.secret_env:
            msg = (
                "previous_secret_env duplicates secret_env. "
                "Fix: use the prior secret's different environment variable or remove it."
            )
            raise ValueError(msg)
        return self


class Limits(VoiceyModel):
    """Per-instance safety and resource limits."""

    max_duration_s: int = 600
    max_concurrent: int = 20
    silence_hangup_s: int = 30
    daily_spend_alert_usd: float | None = None

    @field_validator("max_duration_s")
    @classmethod
    def valid_max_duration(cls, value: int) -> int:
        if not 10 <= value <= 14400:
            msg = (
                "limits.max_duration_s is outside 10-14400. "
                "Fix: choose a duration from 10 seconds through 4 hours."
            )
            raise ValueError(msg)
        return value

    @field_validator("max_concurrent")
    @classmethod
    def valid_concurrency(cls, value: int) -> int:
        if not 1 <= value <= 10000:
            msg = (
                "limits.max_concurrent is outside 1-10000. "
                "Fix: choose at least one concurrent call."
            )
            raise ValueError(msg)
        return value

    @field_validator("silence_hangup_s")
    @classmethod
    def valid_silence_timeout(cls, value: int) -> int:
        if not 5 <= value <= 3600:
            msg = (
                "limits.silence_hangup_s is outside 5-3600. "
                "Fix: choose a timeout from 5 seconds through 1 hour."
            )
            raise ValueError(msg)
        return value

    @field_validator("daily_spend_alert_usd")
    @classmethod
    def valid_spend_alert(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            msg = (
                "limits.daily_spend_alert_usd must be positive. "
                "Fix: provide a positive USD amount or remove the alert."
            )
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def silence_fits_duration(self) -> Limits:
        if self.silence_hangup_s >= self.max_duration_s:
            msg = (
                "silence_hangup_s must be shorter than max_duration_s. "
                "Fix: lower silence_hangup_s or raise max_duration_s."
            )
            raise ValueError(msg)
        return self


class Observability(VoiceyModel):
    """Prometheus and OTLP export settings with secret headers kept in env."""

    prometheus_enabled: bool = False
    prometheus_bind: str = "127.0.0.1"
    prometheus_port: int = 9464
    prometheus_path: str = "/metrics"
    otlp_endpoint: str | None = None
    otlp_headers_env: str | None = None

    @field_validator("prometheus_bind")
    @classmethod
    def valid_metrics_bind(cls, value: str) -> str:
        try:
            return str(ipaddress.ip_address(value))
        except ValueError as exc:
            msg = (
                "observability.prometheus_bind is not an IP address. "
                "Fix: use 127.0.0.1 by default or an explicit interface address."
            )
            raise ValueError(msg) from exc

    @field_validator("prometheus_port")
    @classmethod
    def valid_metrics_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            msg = (
                "observability.prometheus_port is outside 1-65535. "
                "Fix: choose an available TCP port."
            )
            raise ValueError(msg)
        return value

    @field_validator("prometheus_path")
    @classmethod
    def valid_metrics_path(cls, value: str) -> str:
        if (
            not METRICS_PATH_PATTERN.fullmatch(value)
            or value == "/"
            or "//" in value
            or value.endswith("/")
        ):
            msg = (
                "observability.prometheus_path is invalid. "
                "Fix: use a path such as '/metrics' with no trailing slash."
            )
            raise ValueError(msg)
        return value

    @field_validator("otlp_endpoint")
    @classmethod
    def valid_otlp_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "https" and not loopback)
        ):
            msg = (
                "observability.otlp_endpoint is not a safe OTLP/HTTP traces endpoint. "
                "Fix: use HTTPS remotely or HTTP on a loopback collector."
            )
            raise ValueError(msg)
        return value

    @field_validator("otlp_headers_env")
    @classmethod
    def valid_headers_env(cls, value: str | None) -> str | None:
        if value is not None and not ENV_NAME_PATTERN.fullmatch(value):
            msg = (
                "observability.otlp_headers_env is not an environment-variable name. "
                "Fix: use uppercase letters, digits, and underscores."
            )
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def headers_require_export(self) -> Observability:
        if self.otlp_headers_env is not None and self.otlp_endpoint is None:
            msg = (
                "observability.otlp_headers_env requires otlp_endpoint. "
                "Fix: configure the collector endpoint or remove the header env name."
            )
            raise ValueError(msg)
        return self


class Behavior(VoiceyModel):
    """Runtime-mapped conversation and telephony behavior."""

    allow_interruptions: bool = True
    voicemail: VoicemailBehavior = "hangup"
    dtmf: bool = True
    transfer_number: str | None = None
    end_call_phrases: list[str] = Field(default_factory=lambda: ["goodbye", "bye now"])

    @field_validator("transfer_number")
    @classmethod
    def valid_transfer_number(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_e164(value, field_name="behavior.transfer_number")

    @field_validator("end_call_phrases")
    @classmethod
    def valid_end_phrases(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(phrase.casefold().strip() for phrase in values))
        if any(not phrase for phrase in normalized):
            msg = "behavior.end_call_phrases contains a blank phrase. Fix: remove blank entries."
            raise ValueError(msg)
        return normalized


class Agent(VoiceyModel):
    """Canonical agent configuration shared by both runtime bootstraps."""

    name: str
    runtime: RuntimeName
    models: Models
    voice: Voice = Field(default_factory=Voice)
    persona: str
    flow: str
    tools: str | list[ToolReference]
    phone: Phone | None = None
    web: Web = Field(default_factory=Web)
    results: Results
    limits: Limits = Field(default_factory=Limits)
    observability: Observability = Field(default_factory=Observability)
    behavior: Behavior = Field(default_factory=Behavior)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if len(value) > 63 or not re.fullmatch(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", value):
            msg = (
                f"{value!r} is not a valid agent name. "
                "Fix: use at most 63 lowercase letters, digits, and single hyphens."
            )
            raise ValueError(msg)
        return value

    @field_validator("persona")
    @classmethod
    def valid_persona(cls, value: str) -> str:
        if not value or len(value) > 20000:
            msg = (
                "persona must contain 1-20000 characters. "
                "Fix: provide concise agent instructions in that range."
            )
            raise ValueError(msg)
        return value

    @field_validator("flow")
    @classmethod
    def valid_flow_reference(cls, value: str) -> str:
        if not IMPORT_REFERENCE_PATTERN.fullmatch(value):
            msg = (
                f"{value!r} is not a module:attribute reference. "
                "Fix: use a value such as 'flow:entry'."
            )
            raise ValueError(msg)
        return value

    @field_validator("tools")
    @classmethod
    def valid_tool_references(
        cls,
        value: str | list[ToolReference],
    ) -> str | list[ToolReference]:
        references = [value] if isinstance(value, str) else value
        if not references:
            msg = "tools is empty. Fix: provide a module or at least one typed callable."
            raise ValueError(msg)
        for reference in references:
            if isinstance(reference, str):
                if not re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_.]*$", reference):
                    msg = (
                        f"{reference!r} is not a tools module. "
                        "Fix: use a Python module path such as 'tools' or 'app.tools'."
                    )
                    raise ValueError(msg)
            else:
                _callable_reference(reference)
        return value

    @field_serializer("tools")
    def serialize_tools(self, value: str | list[ToolReference]) -> str | list[str]:
        if isinstance(value, str):
            return value
        return [
            reference if isinstance(reference, str) else _callable_reference(reference)
            for reference in value
        ]

    @model_validator(mode="after")
    def channels_and_behavior_are_coherent(self) -> Agent:
        if self.phone is None and not self.web.enabled:
            msg = (
                "no conversation channel is enabled. "
                "Fix: configure phone or set web.enabled=true with allowed_origins."
            )
            raise ValueError(msg)
        if self.phone is None and self.behavior.transfer_number is not None:
            msg = (
                "behavior.transfer_number requires the phone channel. "
                "Fix: configure phone or remove transfer_number."
            )
            raise ValueError(msg)
        return self

    @property
    def config_hash(self) -> str:
        """Hash the sorted canonical JSON wire representation."""
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _validate_model_id(value: str) -> str:
    if not MODEL_ID_PATTERN.fullmatch(value):
        msg = (
            f"{value!r} is not a provider/model id. "
            "Fix: use a catalog id such as 'deepgram/nova-3'."
        )
        raise ValueError(msg)
    return value


def _validate_e164(value: str, *, field_name: str) -> str:
    if not E164_PATTERN.fullmatch(value):
        msg = (
            f"{field_name}={value!r} is not E.164. "
            "Fix: use '+' followed by 8-15 digits, including country code."
        )
        raise ValueError(msg)
    return value


def _callable_reference(function: ToolCallable) -> str:
    module = getattr(function, "__module__", "")
    qualname = getattr(function, "__qualname__", "")
    if (
        not module
        or not qualname
        or "<locals>" in qualname
        or getattr(function, "__name__", "") == "<lambda>"
    ):
        msg = (
            "tool callables must be importable module-level functions. "
            "Fix: move the function out of a local scope and avoid lambdas."
        )
        raise ValueError(msg)
    return f"{module}:{qualname}"
