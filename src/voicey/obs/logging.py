"""Structured logging with call correlation and PII-safe production output."""

from __future__ import annotations

import logging
import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import Any, Literal, TextIO, cast

import structlog
from structlog.contextvars import bound_contextvars, merge_contextvars
from structlog.typing import EventDict, WrappedLogger

LogFormat = Literal["json", "pretty"]

REDACTED = "[REDACTED]"
_SECRET_KEY_PARTS = (
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)
_PII_KEY_PARTS = (
    "address",
    "caller_name",
    "customer_name",
    "e164",
    "email",
    "patient_name",
    "phone",
    "recording",
    "tool_args",
    "tool_result",
    "transcript",
    "utterance",
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_E164 = re.compile(r"(?<!\d)\+[1-9]\d{7,14}(?!\d)")
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_SECRET_VALUE = re.compile(r"(?i)\b(?:whsec_|sk-|api[_-]?key[=:]\s*)[A-Za-z0-9._~+/=-]{6,}")
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "exception": logging.ERROR,
    "critical": logging.CRITICAL,
}


def configure_logging(
    *,
    format: LogFormat = "json",
    level: int | str = logging.INFO,
    stream: TextIO | None = None,
) -> None:
    """Configure structlog for production JSON or a human-readable dev stream."""
    renderer: structlog.types.Processor
    if format == "json":
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False, sort_keys=True)

    structlog.configure(
        processors=[
            merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_sensitive_data,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        cache_logger_on_first_use=False,
    )


def get_logger(**initial_values: Any) -> structlog.typing.FilteringBoundLogger:
    """Return the configured structured logger."""
    return cast(
        "structlog.typing.FilteringBoundLogger",
        structlog.get_logger(**initial_values),
    )


@contextmanager
def call_context(
    call_id: str,
    *,
    config_hash: str | None = None,
    runtime: str | None = None,
) -> Generator[None]:
    """Bind correlation fields to the current async/thread context only."""
    values = {"call_id": call_id}
    if config_hash is not None:
        values["config_hash"] = config_hash
    if runtime is not None:
        values["runtime"] = runtime
    with bound_contextvars(**values):
        yield


def redact_sensitive_data(
    _logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Remove secrets always and PII from info-or-higher log events."""
    numeric_level = _LEVELS.get(method_name, logging.INFO)
    redact_pii = numeric_level >= logging.INFO
    return {
        key: _sanitize_value(key, value, redact_pii=redact_pii) for key, value in event_dict.items()
    }


def scrub_secrets(value: object) -> object:
    """Recursively redact secret-shaped values while retaining record PII."""
    return _sanitize_value("", value, redact_pii=False)


def _sanitize_value(key: str, value: Any, *, redact_pii: bool) -> Any:
    normalized_key = key.casefold()
    if any(marker in normalized_key for marker in _SECRET_KEY_PARTS):
        return REDACTED
    if redact_pii and (
        any(marker in normalized_key for marker in _PII_KEY_PARTS)
        or normalized_key in {"args", "result"}
    ):
        return REDACTED
    if redact_pii and normalized_key in {"exc_info", "exception"}:
        return REDACTED
    if isinstance(value, Mapping):
        nested_mapping = cast("Mapping[object, object]", value)
        return {
            str(nested_key): _sanitize_value(
                str(nested_key),
                nested_value,
                redact_pii=redact_pii,
            )
            for nested_key, nested_value in nested_mapping.items()
        }
    if isinstance(value, list | tuple):
        nested_sequence = cast("list[object] | tuple[object, ...]", value)
        return [
            _sanitize_value(key, nested_value, redact_pii=redact_pii)
            for nested_value in nested_sequence
        ]
    if isinstance(value, str):
        return _sanitize_string(value, redact_pii=redact_pii)
    return value


def _sanitize_string(value: str, *, redact_pii: bool) -> str:
    sanitized = _BEARER.sub(REDACTED, value)
    sanitized = _SECRET_VALUE.sub(REDACTED, sanitized)
    if redact_pii:
        sanitized = _EMAIL.sub(REDACTED, sanitized)
        sanitized = _E164.sub(REDACTED, sanitized)
    return sanitized
