"""Stable application errors backed by the public voicekit error catalog."""

from __future__ import annotations

from dataclasses import dataclass

ERROR_DOCS_BASE = "https://github.com/voicekit/voicekit/blob/main/docs/errors.md"


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
        fix="Resolve the reported repository error before accepting more calls.",
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
    "VK-OBS-006": ErrorDefinition(
        code="VK-OBS-006",
        cause="Prometheus or OTLP observability could not be configured or served.",
        fix="Correct the observability endpoint, header env, bind address, or port and retry.",
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
    "VK-REL-001": ErrorDefinition(
        code="VK-REL-001",
        cause="The results-relay URL, credential, or protocol setting is invalid.",
        fix="Correct the relay configuration and rotate any exposed credential.",
    ),
    "VK-REL-002": ErrorDefinition(
        code="VK-REL-002",
        cause="The results relay is unavailable or failed its startup readiness check.",
        fix="Restore the relay before allowing the cloud worker to accept calls.",
    ),
    "VK-REL-003": ErrorDefinition(
        code="VK-REL-003",
        cause="A results-relay request is unsigned, expired, replayed, or invalid.",
        fix="Synchronize clocks and use a current relay credential to sign a fresh request.",
    ),
    "VK-REL-004": ErrorDefinition(
        code="VK-REL-004",
        cause="A results-relay fencing token is invalid, expired, or stale.",
        fix="Stop the stale worker and resume with the server-issued current generation.",
    ),
    "VK-REL-005": ErrorDefinition(
        code="VK-REL-005",
        cause="A results-relay operation is out of order or reuses an idempotency key.",
        fix="Retry the same operation bytes or resume at the server-reported sequence.",
    ),
    "VK-REL-006": ErrorDefinition(
        code="VK-REL-006",
        cause="The durable results-relay journal or backing repository failed.",
        fix="Stop worker admission, restore durable relay storage, and retry recovery.",
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
    "VK-ART-003": ErrorDefinition(
        code="VK-ART-003",
        cause="Durable object storage is misconfigured, unreachable, or failed preflight.",
        fix=(
            "Correct the private bucket, HTTPS endpoint, credentials, and region, "
            "then rerun deploy."
        ),
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
    "VK-TEL-012": ErrorDefinition(
        code="VK-TEL-012",
        cause="A warm-transfer handoff did not reach one safe, confirmed conference state.",
        fix=(
            "Keep the caller with the agent, inspect the reported transfer id, "
            "and retry only after its provider state is terminal."
        ),
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
    "VK-RUN-008": ErrorDefinition(
        code="VK-RUN-008",
        cause="The runtime is draining and cannot expose a new call.",
        fix=(
            "Route the new call to the ready generation and let this generation "
            "finish existing calls."
        ),
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
    "VK-WEB-001": ErrorDefinition(
        code="VK-WEB-001",
        cause="A browser session token is missing, expired, replayed, or has the wrong scope.",
        fix="Return to the local playground and start a new browser session.",
    ),
    "VK-WEB-002": ErrorDefinition(
        code="VK-WEB-002",
        cause="A browser origin, public host, or forwarded-header chain is not trusted.",
        fix="Use an allowed origin and configure every trusted proxy plus the exact public URL.",
    ),
    "VK-WEB-003": ErrorDefinition(
        code="VK-WEB-003",
        cause="Browser-session issuance or signaling exceeded its abuse limit.",
        fix="Wait for the printed retry interval or end an active session before retrying.",
    ),
    "VK-WEB-004": ErrorDefinition(
        code="VK-WEB-004",
        cause="An admin or session-issuance request lacks the configured integrator credential.",
        fix="Use the local admin listener or supply the configured integrator authorization.",
    ),
    "VK-WEB-005": ErrorDefinition(
        code="VK-WEB-005",
        cause="The embedded playground assets or development reload could not be prepared safely.",
        fix="Rebuild the wheel assets or fix the reported project module before retrying.",
    ),
    "VK-DEP-001": ErrorDefinition(
        code="VK-DEP-001",
        cause="A deployment artifact conflicts with an existing project file.",
        fix="Review or move the reported file; voicekit never overwrites deployment edits.",
    ),
    "VK-DEP-002": ErrorDefinition(
        code="VK-DEP-002",
        cause="The deployment persistence topology is unsafe or incompatible.",
        fix=(
            "Use the target's documented storage backend, local volume, and replica topology, "
            "then rerun the persistence preflight."
        ),
    ),
    "VK-DEP-003": ErrorDefinition(
        code="VK-DEP-003",
        cause="The production container runtime configuration is incomplete or invalid.",
        fix="Set every documented deployment environment variable, then rerun `voicekit doctor`.",
    ),
    "VK-DEP-004": ErrorDefinition(
        code="VK-DEP-004",
        cause="Post-deploy smoke verification failed or lacks a required live-call input.",
        fix="Correct the printed URL/number/credential requirement and rerun the smoke command.",
    ),
    "VK-DEP-005": ErrorDefinition(
        code="VK-DEP-005",
        cause="Docker or Compose could not validate the generated deployment.",
        fix="Install a supported Docker Compose release, fix the reported validation, and retry.",
    ),
    "VK-DEP-006": ErrorDefinition(
        code="VK-DEP-006",
        cause="A managed deployment CLI is missing, unauthenticated, timed out, or failed.",
        fix="Install and authenticate the printed platform CLI, then rerun the same command.",
    ),
    "VK-DEP-007": ErrorDefinition(
        code="VK-DEP-007",
        cause="Managed deployment resource identity, ownership, or checkpoint evidence drifted.",
        fix=(
            "Inspect the platform resources and owner-only resource ledger; explicitly adopt "
            "only verified resources or restore the recorded identity before retrying."
        ),
    ),
    "VK-DEP-008": ErrorDefinition(
        code="VK-DEP-008",
        cause="A cloud-worker build, runtime bootstrap, or explicit deployment plan is invalid.",
        fix=(
            "Correct the named runtime, image, region, relay, wheel, project, or secret "
            "input and regenerate the cloud artifacts."
        ),
    ),
    "VK-DEP-009": ErrorDefinition(
        code="VK-DEP-009",
        cause=(
            "A required Pipecat Cloud or LiveKit Cloud CLI is missing, "
            "unauthenticated, timed out, or failed."
        ),
        fix=(
            "Install and authenticate the pinned platform CLI, inspect its direct output, "
            "then rerun the same resumable voicekit command."
        ),
    ),
    "VK-DEP-010": ErrorDefinition(
        code="VK-DEP-010",
        cause="Cloud agent ownership, secret-sync, deployment, or rollback evidence is unsafe.",
        fix=(
            "Inspect the owner-only cloud ledger and platform agent; adopt the exact id "
            "explicitly or restore the ledgered version before retrying."
        ),
    ),
    "VK-TST-001": ErrorDefinition(
        code="VK-TST-001",
        cause="A simulated-caller scenario, profile, filter, or test config is invalid.",
        fix="Correct the reported tests/scenarios source or tests/voicekit-test.jsonc field.",
    ),
    "VK-TST-002": ErrorDefinition(
        code="VK-TST-002",
        cause="A native runtime evaluator is unavailable or rejected generated test inputs.",
        fix="Install the selected runtime extra and use the pinned version shown by doctor.",
    ),
    "VK-TST-003": ErrorDefinition(
        code="VK-TST-003",
        cause="A configured test tier, sim-caller model, or judge could not execute.",
        fix="Start the configured local model or supply the documented cloud/live prerequisites.",
    ),
    "VK-TST-004": ErrorDefinition(
        code="VK-TST-004",
        cause="One or more voice-agent scenarios failed a hard or judged assertion.",
        fix="Review the transcript, cited judge reason, and stability attempts before retrying.",
    ),
    "VK-TST-005": ErrorDefinition(
        code="VK-TST-005",
        cause="A soak run is invalid or exceeded a call, file-descriptor, or memory bound.",
        fix=(
            "Inspect the soak report, fix the leaked resource or terminal call, "
            "then rerun the same duration and concurrency."
        ),
    ),
    "VK-CLI-001": ErrorDefinition(
        code="VK-CLI-001",
        cause="A non-interactive command is missing an explicit required choice.",
        fix="Pass the exact flag named by the error or rerun in an interactive terminal.",
    ),
    "VK-CLI-002": ErrorDefinition(
        code="VK-CLI-002",
        cause="The guided setup was cancelled, conflicted, or cannot be resumed safely.",
        fix="Rerun `voicekit init --resume` or choose a new empty project directory.",
    ),
    "VK-CLI-003": ErrorDefinition(
        code="VK-CLI-003",
        cause="A project scaffold or protected environment file could not be written safely.",
        fix="Check project permissions and disk space, then resume `voicekit init`.",
    ),
    "VK-CLI-004": ErrorDefinition(
        code="VK-CLI-004",
        cause="A required provider key is missing, invalid, or could not be verified.",
        fix="Run the printed `voicekit keys add <provider>` command and validate again.",
    ),
    "VK-CLI-005": ErrorDefinition(
        code="VK-CLI-005",
        cause="The requested runtime, carrier, recipe, deploy target, or extra is unavailable.",
        fix="Choose an enabled capability or install the exact extra printed by the command.",
    ),
    "VK-CLI-006": ErrorDefinition(
        code="VK-CLI-006",
        cause="One or more doctor preflight checks failed.",
        fix="Apply each printed advice line, then rerun `voicekit doctor`.",
    ),
    "VK-CLI-007": ErrorDefinition(
        code="VK-CLI-007",
        cause="The command cannot run from the current project or lifecycle state.",
        fix="Run the printed next step or resume the interrupted command first.",
    ),
    "VK-CLI-008": ErrorDefinition(
        code="VK-CLI-008",
        cause=(
            "A consequential package, money-spending, or live-routing mutation lacks confirmation."
        ),
        fix="Review the exact mutation, then confirm interactively or pass `--yes`.",
    ),
    "VK-CLI-009": ErrorDefinition(
        code="VK-CLI-009",
        cause="A CLI operation failed without a safe provider-specific mapping.",
        fix="Retry with `--verbose`; if it repeats, use the printed pre-filled issue link.",
    ),
    "VK-CLI-010": ErrorDefinition(
        code="VK-CLI-010",
        cause="A command output request or filter is malformed.",
        fix="Correct the reported flag, identifier, or JSON/output option and retry.",
    ),
    "VK-UPG-001": ErrorDefinition(
        code="VK-UPG-001",
        cause="The project or installed uv version cannot support a safe voicekit upgrade.",
        fix="Use a regular uv-managed project with a direct voicekit dependency and uv >=0.11,<1.",
    ),
    "VK-UPG-002": ErrorDefinition(
        code="VK-UPG-002",
        cause="The lockfile-only voicekit upgrade or fresh-process verification failed.",
        fix="Review the safe error detail; the prior lockfile was restored when available.",
    ),
    "VK-UPG-003": ErrorDefinition(
        code="VK-UPG-003",
        cause="Recipe baseline metadata or source drift is invalid or unsafe to compare.",
        fix="Restore the tracked recipe baseline and regular recipe-owned files, then retry.",
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


def error_docs_url(code: str) -> str:
    """Return the stable public catalog anchor for a registered error."""
    if code not in ERROR_CATALOG:
        msg = f"unregistered voicekit error code: {code}"
        raise AssertionError(msg)
    return f"{ERROR_DOCS_BASE}#{code.casefold()}"
