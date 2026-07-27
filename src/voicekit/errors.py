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
    "VK-RES-006": ErrorDefinition(
        code="VK-RES-006",
        cause="The call owner no longer holds the current fencing generation.",
        fix="Stop the stale worker; only the current lease owner may mutate the call.",
    ),
    "VK-RES-007": ErrorDefinition(
        code="VK-RES-007",
        cause="The call is terminal but its immutable terminal event is missing.",
        fix="Stop accepting calls and restore the repository from a consistent backup.",
    ),
    "VK-RES-008": ErrorDefinition(
        code="VK-RES-008",
        cause="A lifecycle, outbox, or retention transaction failed.",
        fix="Resolve the reported storage condition before accepting more calls.",
    ),
    "VK-RES-009": ErrorDefinition(
        code="VK-RES-009",
        cause="The requested immutable result event does not exist.",
        fix="Run `voicekit calls list` and retry with an existing call or event id.",
    ),
    "VK-RES-010": ErrorDefinition(
        code="VK-RES-010",
        cause="A recording update is missing, premature, or not engine-owned.",
        fix="Retry after terminal persistence with the stable engine recording id.",
    ),
    "VK-ART-001": ErrorDefinition(
        code="VK-ART-001",
        cause="An artifact key escapes protected storage or targets a symbolic link.",
        fix="Use a relative recordings/ or backups/ key with no parent traversal.",
    ),
    "VK-ART-002": ErrorDefinition(
        code="VK-ART-002",
        cause="A protected artifact could not be written, read, or deleted.",
        fix="Check artifact-store permissions and disk health, then retry.",
    ),
    "VK-TOL-001": ErrorDefinition(
        code="VK-TOL-001",
        cause="A callable is not registered as a voicekit tool.",
        fix="Decorate the typed function with @tool before registering it.",
    ),
    "VK-TOL-002": ErrorDefinition(
        code="VK-TOL-002",
        cause="A Python tool declaration cannot produce a safe JSON schema.",
        fix="Use a valid name, typed non-variadic parameters, and a return annotation.",
    ),
    "VK-TOL-003": ErrorDefinition(
        code="VK-TOL-003",
        cause="The tool executor configuration is invalid.",
        fix="Use a positive execution timeout and retry the command.",
    ),
    "VK-TOL-004": ErrorDefinition(
        code="VK-TOL-004",
        cause="An HTTP tool is misconfigured or its remote request failed.",
        fix="Check the method, URL parameters, environment credentials, and endpoint.",
    ),
    "VK-TOL-005": ErrorDefinition(
        code="VK-TOL-005",
        cause="A final tool observation could not be persisted.",
        fix="Stop accepting calls and restore protected observation storage before retrying.",
    ),
    "VK-TEL-001": ErrorDefinition(
        code="VK-TEL-001",
        cause="The requested telephony adapter or its optional dependency is unavailable.",
        fix='Install the exact carrier extra, for example `uv pip install "voicekit[twilio]"`.',
    ),
    "VK-TEL-002": ErrorDefinition(
        code="VK-TEL-002",
        cause="A telephony target, credential, number, or adapter setting is invalid.",
        fix="Correct the reported carrier setting and run `voicekit doctor` before retrying.",
    ),
    "VK-TEL-003": ErrorDefinition(
        code="VK-TEL-003",
        cause="A carrier phone number lookup returned no unique result.",
        fix="List owned numbers, select one exact E.164 number, and retry.",
    ),
    "VK-TEL-004": ErrorDefinition(
        code="VK-TEL-004",
        cause="The carrier definitively rejected an operation.",
        fix="Resolve the safe carrier status/code reported by doctor, then retry explicitly.",
    ),
    "VK-TEL-005": ErrorDefinition(
        code="VK-TEL-005",
        cause="A durable telephony routing snapshot or outbound intent could not be stored.",
        fix="Stop carrier mutations and restore the protected telephony ledger.",
    ),
    "VK-TEL-006": ErrorDefinition(
        code="VK-TEL-006",
        cause="Carrier routing changed after voicekit applied its temporary target.",
        fix=(
            "Review the current carrier route; restore manually or retry with an explicit snapshot."
        ),
    ),
    "VK-TEL-007": ErrorDefinition(
        code="VK-TEL-007",
        cause="An outbound carrier operation has an ambiguous outcome and was not retried.",
        fix="Reconcile the reported intent id before deciding whether to place another call.",
    ),
    "VK-TEL-008": ErrorDefinition(
        code="VK-TEL-008",
        cause="A signed carrier callback has an unknown or incomplete event shape.",
        fix="Inspect safe callback metadata and update the carrier mapping before accepting calls.",
    ),
    "VK-TEL-009": ErrorDefinition(
        code="VK-TEL-009",
        cause="An authenticated carrier recording could not be downloaded safely.",
        fix="Check the recording SID, account credentials, size limit, and carrier availability.",
    ),
    "VK-TEL-010": ErrorDefinition(
        code="VK-TEL-010",
        cause="A carrier media frame violates the certified codec or stream protocol.",
        fix="Reject the frame and inspect the carrier media-stream configuration.",
    ),
    "VK-TEL-011": ErrorDefinition(
        code="VK-TEL-011",
        cause="The carrier API is unavailable or returned an indeterminate infrastructure error.",
        fix="Check carrier status and connectivity; reconcile mutations before any explicit retry.",
    ),
    "VK-RUN-001": ErrorDefinition(
        code="VK-RUN-001",
        cause="The selected runtime or one of its pinned optional dependencies is unavailable.",
        fix="Install the runtime extra printed by the command and rerun `voicekit doctor`.",
    ),
    "VK-RUN-002": ErrorDefinition(
        code="VK-RUN-002",
        cause="A runtime provider model, credential, or voice setting cannot be constructed.",
        fix="Correct the reported provider setting or key, then rerun `voicekit doctor`.",
    ),
    "VK-RUN-003": ErrorDefinition(
        code="VK-RUN-003",
        cause="The native runtime flow entrypoint is missing, invalid, or failed to initialize.",
        fix="Export the documented native flow entrypoint and run its runtime tests.",
    ),
    "VK-RUN-004": ErrorDefinition(
        code="VK-RUN-004",
        cause="The runtime cannot admit another call at the configured concurrency limit.",
        fix=(
            "Wait for an active call to finish or raise limits.max_concurrent with enough capacity."
        ),
    ),
    "VK-RUN-005": ErrorDefinition(
        code="VK-RUN-005",
        cause="A media connection is not bound to a valid answer-time call reservation.",
        fix="Reject the connection and verify the signed carrier or web-session handshake.",
    ),
    "VK-RUN-006": ErrorDefinition(
        code="VK-RUN-006",
        cause="The runtime call lifecycle could not be started, renewed, or finalized safely.",
        fix="Stop admitting calls, restore durable storage, then run the recovery command.",
    ),
    "VK-RUN-007": ErrorDefinition(
        code="VK-RUN-007",
        cause="The runtime host received a malformed or unauthenticated signaling request.",
        fix="Verify the expected public URL, proxy trust, signature, and signaling payload.",
    ),
    "VK-TUN-001": ErrorDefinition(
        code="VK-TUN-001",
        cause="The selected tunnel provider dependency or executable is unavailable.",
        fix="Install the exact tunnel extra or executable named by the error, then retry.",
    ),
    "VK-TUN-002": ErrorDefinition(
        code="VK-TUN-002",
        cause="The tunnel provider, local port, public URL, or protocol is invalid.",
        fix="Use a supported provider, a free TCP port, and an HTTPS public origin.",
    ),
    "VK-TUN-003": ErrorDefinition(
        code="VK-TUN-003",
        cause="The tunnel process exited or did not publish a valid public URL in time.",
        fix="Check provider connectivity and safe tunnel diagnostics, then retry.",
    ),
    "VK-TUN-004": ErrorDefinition(
        code="VK-TUN-004",
        cause="The public tunnel failed its authenticated WebSocket round-trip probe.",
        fix="Do not point a carrier; repair WebSocket upgrade forwarding and rerun doctor.",
    ),
    "VK-TUN-005": ErrorDefinition(
        code="VK-TUN-005",
        cause="The tunnel process or listener could not be shut down cleanly.",
        fix="Stop the reported tunnel process before starting another development session.",
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
