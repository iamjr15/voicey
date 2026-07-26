"""Stable application errors backed by the public voicekit error catalog."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ErrorDefinition:
    """User-facing definition for one stable error code."""

    code: str
    cause: str
    fix: str


ERROR_CATALOG: dict[str, ErrorDefinition] = {
    "VK-CFG-001": ErrorDefinition(
        code="VK-CFG-001",
        cause="The agent configuration is incomplete or incompatible.",
        fix="Apply every listed configuration fix, then rerun validation.",
    ),
    "VK-CFG-002": ErrorDefinition(
        code="VK-CFG-002",
        cause="The voicekit.jsonc manifest could not be read or validated.",
        fix="Correct the reported manifest field or resume voicekit init.",
    ),
    "VK-CFG-003": ErrorDefinition(
        code="VK-CFG-003",
        cause="The voicekit.jsonc manifest could not be saved atomically.",
        fix="Check project-directory permissions and available disk space, then retry.",
    ),
    "VK-OBS-001": ErrorDefinition(
        code="VK-OBS-001",
        cause="The protected call-record database could not be opened or closed.",
        fix="Check the data-directory permissions and disk health, then retry.",
    ),
    "VK-OBS-002": ErrorDefinition(
        code="VK-OBS-002",
        cause="A call-record observation could not be committed durably.",
        fix="Resolve the reported SQLite error before accepting more calls.",
    ),
    "VK-OBS-003": ErrorDefinition(
        code="VK-OBS-003",
        cause="The requested call record does not exist.",
        fix="Run `voicekit calls list` and retry with an existing call id.",
    ),
    "VK-OBS-004": ErrorDefinition(
        code="VK-OBS-004",
        cause="The call-record schema is newer or incompatible.",
        fix="Install the matching voicekit version or run its documented upgrade.",
    ),
    "VK-OBS-005": ErrorDefinition(
        code="VK-OBS-005",
        cause="The requested call-record query is outside safe limits.",
        fix="Choose a result limit from 1 through 1000.",
    ),
    "VK-SEC-001": ErrorDefinition(
        code="VK-SEC-001",
        cause="A protected local path has unsafe permissions.",
        fix="Restore owner-only permissions and rerun the command.",
    ),
    "VK-SEC-002": ErrorDefinition(
        code="VK-SEC-002",
        cause="A protected local file path is a symbolic link.",
        fix="Replace the link with a regular file inside the protected data directory.",
    ),
    "VK-RES-001": ErrorDefinition(
        code="VK-RES-001",
        cause="A webhook secret is not a valid whsec_ value.",
        fix="Generate or paste a whsec_-prefixed base64 webhook secret.",
    ),
    "VK-RES-002": ErrorDefinition(
        code="VK-RES-002",
        cause="Required Standard Webhooks headers are missing or malformed.",
        fix="Forward webhook-id, webhook-timestamp, and webhook-signature unchanged.",
    ),
    "VK-RES-003": ErrorDefinition(
        code="VK-RES-003",
        cause="The webhook timestamp is outside the replay-tolerance window.",
        fix="Correct receiver clock skew and reject the replayed request.",
    ),
    "VK-RES-004": ErrorDefinition(
        code="VK-RES-004",
        cause="The Standard Webhooks signature does not match the raw request body.",
        fix="Verify the raw body with the current or previous whsec_ secret.",
    ),
    "VK-RES-005": ErrorDefinition(
        code="VK-RES-005",
        cause="results.set() was called outside an active call context.",
        fix="Call results.set() only from flow or tool code running for an active call.",
    ),
    "VK-TOL-001": ErrorDefinition(
        code="VK-TOL-001",
        cause="A callable is not registered as a voicekit tool.",
        fix="Decorate the typed function with @tool before registering it.",
    ),
}


class VoicekitError(RuntimeError):
    """An expected application error with a stable catalog code."""

    def __init__(self, code: str, *, detail: str | None = None) -> None:
        try:
            definition = ERROR_CATALOG[code]
        except KeyError as exc:
            msg = f"unregistered voicekit error code: {code}"
            raise AssertionError(msg) from exc
        self.code = code
        self.definition = definition
        self.detail = detail
        message = f"{code}: {definition.cause}"
        if detail:
            message = f"{message} {detail}"
        message = f"{message} Fix: {definition.fix}"
        super().__init__(message)
