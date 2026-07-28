# Error catalog

Every expected voicekit failure has a stable code, a safe cause, and an
actionable fix. CLI errors link to the matching anchor on this page. Details
may add non-secret context, but never replace the stable contract below.

An error not represented here is a product bug. The CLI reports it as
`VK-CLI-009` with a pre-filled issue link.

## VK-CFG-001

**Cause:** The agent configuration is incomplete or incompatible.

**Fix:** Apply every listed configuration fix, then rerun validation.

## VK-CFG-002

**Cause:** The `voicekit.jsonc` manifest could not be read or validated.

**Fix:** Correct the reported manifest field or resume `voicekit init`.

## VK-CFG-003

**Cause:** The `voicekit.jsonc` manifest could not be saved atomically.

**Fix:** Check project-directory permissions and available disk space, then retry.

## VK-OBS-001

**Cause:** The protected call-record database could not be opened or closed.

**Fix:** Check the data-directory permissions and disk health, then retry.

## VK-OBS-002

**Cause:** A call-record observation could not be committed durably.

**Fix:** Resolve the reported repository error before accepting more calls.

## VK-OBS-003

**Cause:** The requested call record does not exist.

**Fix:** Run `voicekit calls list` and retry with an existing call id.

## VK-OBS-004

**Cause:** The call-record schema is newer or incompatible.

**Fix:** Install the matching voicekit version or run its documented upgrade.

## VK-OBS-005

**Cause:** The requested call-record query is outside safe limits.

**Fix:** Choose a result limit from 1 through 1000.

## VK-SEC-001

**Cause:** A protected local path has unsafe permissions.

**Fix:** Restore owner-only permissions and rerun the command.

## VK-SEC-002

**Cause:** A protected local file path is a symbolic link.

**Fix:** Replace the link with a regular file inside the protected data directory.

## VK-RES-001

**Cause:** A webhook secret is not a valid `whsec_` value.

**Fix:** Generate or paste a `whsec_`-prefixed base64 webhook secret.

## VK-RES-002

**Cause:** Required Standard Webhooks headers are missing or malformed.

**Fix:** Forward `webhook-id`, `webhook-timestamp`, and `webhook-signature`
unchanged.

## VK-RES-003

**Cause:** The webhook timestamp is outside the replay-tolerance window.

**Fix:** Correct receiver clock skew and reject the replayed request.

## VK-RES-004

**Cause:** The Standard Webhooks signature does not match the raw request body.

**Fix:** Verify the raw body with the current or previous `whsec_` secret.

## VK-RES-005

**Cause:** `results.set()` was called outside an active call context.

**Fix:** Call `results.set()` only from flow or tool code running for an active call.

## VK-RES-006

**Cause:** The call owner no longer holds the current fencing generation.

**Fix:** Stop the stale worker; only the current lease owner may mutate the call.

## VK-RES-007

**Cause:** The call is terminal but its immutable terminal event is missing.

**Fix:** Stop accepting calls and restore the repository from a consistent backup.

## VK-RES-008

**Cause:** A lifecycle, outbox, or retention transaction failed.

**Fix:** Resolve the reported storage condition before accepting more calls.

## VK-RES-009

**Cause:** The requested immutable result event does not exist.

**Fix:** Run `voicekit calls list` and retry with an existing call or event id.

## VK-RES-010

**Cause:** A recording update is missing, premature, or not engine-owned.

**Fix:** Retry after terminal persistence with the stable engine recording id.

## VK-REL-001

**Cause:** The results-relay URL, credential, or protocol setting is invalid.

**Fix:** Correct the relay configuration and rotate any exposed credential.

## VK-REL-002

**Cause:** The results relay is unavailable or failed its startup readiness
check.

**Fix:** Restore the relay before allowing the cloud worker to accept calls.

## VK-REL-003

**Cause:** A results-relay request is unsigned, expired, replayed, or invalid.

**Fix:** Synchronize clocks and use a current relay credential to sign a fresh
request.

## VK-REL-004

**Cause:** A results-relay fencing token is invalid, expired, or stale.

**Fix:** Stop the stale worker and resume with the server-issued current
generation.

## VK-REL-005

**Cause:** A results-relay operation is out of order or reuses an idempotency
key.

**Fix:** Retry the same operation bytes or resume at the server-reported
sequence.

## VK-REL-006

**Cause:** The durable results-relay journal or backing repository failed.

**Fix:** Stop worker admission, restore durable relay storage, and retry
recovery.

## VK-ART-001

**Cause:** An artifact key escapes protected storage or targets a symbolic link.

**Fix:** Use a relative `recordings/` or `backups/` key with no parent traversal.

## VK-ART-002

**Cause:** A protected artifact could not be written, read, or deleted.

**Fix:** Check artifact-store permissions and disk health, then retry.

## VK-ART-003

**Cause:** Durable object storage is misconfigured, unreachable, or failed
preflight.

**Fix:** Correct the private bucket, HTTPS endpoint, credentials, and region,
then rerun deploy.

## VK-TOL-001

**Cause:** A callable is not registered as a voicekit tool.

**Fix:** Decorate the typed function with `@tool` before registering it.

## VK-TOL-002

**Cause:** A Python tool declaration cannot produce a safe JSON schema.

**Fix:** Use a valid name, typed non-variadic parameters, and a return annotation.

## VK-TOL-003

**Cause:** The tool executor configuration is invalid.

**Fix:** Use a positive execution timeout and retry the command.

## VK-TOL-004

**Cause:** An HTTP tool is misconfigured or its remote request failed.

**Fix:** Check the method, URL parameters, environment credentials, and endpoint.

## VK-TOL-005

**Cause:** A final tool observation could not be persisted.

**Fix:** Stop accepting calls and restore protected observation storage before
retrying.

## VK-TEL-001

**Cause:** The requested telephony adapter or optional dependency is unavailable.

**Fix:** Install the exact carrier extra, for example
`uv pip install "voicekit[twilio]"`.

## VK-TEL-002

**Cause:** A telephony target, credential, number, or adapter setting is invalid.

**Fix:** Correct the reported carrier setting and run `voicekit doctor`.

## VK-TEL-003

**Cause:** A carrier phone-number lookup returned no unique result.

**Fix:** List owned numbers, select one exact E.164 number, and retry.

## VK-TEL-004

**Cause:** The carrier definitively rejected an operation.

**Fix:** Resolve the safe carrier status/code reported by doctor, then retry
explicitly.

## VK-TEL-005

**Cause:** A durable routing snapshot or outbound intent could not be stored.

**Fix:** Stop carrier mutations and restore the protected telephony ledger.

## VK-TEL-006

**Cause:** Carrier routing changed after voicekit applied its temporary target.

**Fix:** Review the current route; restore manually or retry with an explicit
snapshot.

## VK-TEL-007

**Cause:** An outbound carrier operation has an ambiguous outcome and was not
retried.

**Fix:** Reconcile the reported intent id before placing another call.

## VK-TEL-008

**Cause:** A signed carrier callback has an unknown or incomplete event shape.

**Fix:** Inspect safe callback metadata and update the carrier mapping.

## VK-TEL-009

**Cause:** An authenticated carrier recording could not be downloaded safely.

**Fix:** Check the recording SID, credentials, size limit, and carrier availability.

## VK-TEL-010

**Cause:** A carrier media frame violates the certified codec or stream protocol.

**Fix:** Reject the frame and inspect the carrier media-stream configuration.

## VK-TEL-011

**Cause:** The carrier API is unavailable or returned an indeterminate
infrastructure error.

**Fix:** Check carrier status and connectivity; reconcile mutations before retrying.

## VK-RUN-001

**Cause:** The selected runtime or a pinned optional dependency is unavailable.

**Fix:** Install the runtime extra printed by the command and rerun doctor.

## VK-RUN-002

**Cause:** A runtime provider model, credential, or voice cannot be constructed.

**Fix:** Correct the reported provider setting or key, then rerun doctor.

## VK-RUN-003

**Cause:** The native flow entrypoint is missing, invalid, or failed to initialize.

**Fix:** Export the documented native flow entrypoint and run its runtime tests.

## VK-RUN-004

**Cause:** The runtime cannot admit another call at the concurrency limit.

**Fix:** Wait for a call to finish or raise `limits.max_concurrent` with capacity.

## VK-RUN-005

**Cause:** A media connection is not bound to a valid answer-time reservation.

**Fix:** Reject it and verify the signed carrier or web-session handshake.

## VK-RUN-006

**Cause:** Runtime call lifecycle could not start, renew, or finalize safely.

**Fix:** Stop admitting calls, restore durable storage, then run recovery.

## VK-RUN-007

**Cause:** Runtime signaling was malformed or unauthenticated.

**Fix:** Verify public URL, proxy trust, signature, and signaling payload.

## VK-RUN-008

**Cause:** The runtime is draining and cannot expose a new call.

**Fix:** Route the new call to the ready generation and let this generation
finish existing calls.

## VK-TUN-001

**Cause:** The selected tunnel dependency or executable is unavailable.

**Fix:** Install the exact tunnel extra or executable named by the error.

## VK-TUN-002

**Cause:** The tunnel provider, local port, public URL, or protocol is invalid.

**Fix:** Use a supported provider, a free port, and an HTTPS public origin.

## VK-TUN-003

**Cause:** The tunnel exited or did not publish a valid URL in time.

**Fix:** Check provider connectivity and safe tunnel diagnostics, then retry.

## VK-TUN-004

**Cause:** The public tunnel failed its authenticated WebSocket probe.

**Fix:** Do not point a carrier; repair WebSocket upgrades and rerun doctor.

## VK-TUN-005

**Cause:** The tunnel process or listener could not shut down cleanly.

**Fix:** Stop the reported tunnel process before starting another session.

## VK-WEB-001

**Cause:** A browser session token is missing, expired, replayed, or has the
wrong scope.

**Fix:** Return to the local playground and start a new browser session.

## VK-WEB-002

**Cause:** A browser origin, public host, or forwarded-header chain is not
trusted.

**Fix:** Use an allowed origin and configure every trusted proxy plus the exact
public URL.

## VK-WEB-003

**Cause:** Browser-session issuance or signaling exceeded its abuse limit.

**Fix:** Wait for the printed retry interval or end an active session before
retrying.

## VK-WEB-004

**Cause:** An admin or session-issuance request lacks the configured integrator
credential.

**Fix:** Use the local admin listener or supply the configured integrator
authorization.

## VK-WEB-005

**Cause:** The embedded playground assets or development reload could not be
prepared safely.

**Fix:** Rebuild the wheel assets or fix the reported project module before
retrying.

## VK-DEP-001

**Cause:** A deployment artifact conflicts with an existing project file.

**Fix:** Review or move the reported file; voicekit never overwrites deployment
edits.

## VK-DEP-002

**Cause:** The deployment persistence topology is unsafe or incompatible.

**Fix:** Use the target's documented storage backend, local volume, and replica
topology, then rerun the persistence preflight.

## VK-DEP-003

**Cause:** The production container runtime configuration is incomplete or
invalid.

**Fix:** Set every documented deployment environment variable, then rerun
`voicekit doctor`.

## VK-DEP-004

**Cause:** Post-deploy smoke verification failed or lacks a required live-call
input.

**Fix:** Correct the printed URL, number, or credential requirement and rerun
the smoke command.

## VK-DEP-005

**Cause:** Docker or Compose could not validate the generated deployment.

**Fix:** Install a supported Docker Compose release, fix the reported
validation, and retry.

## VK-DEP-006

**Cause:** A managed deployment CLI is missing, unauthenticated, timed out, or
failed.

**Fix:** Install and authenticate the printed platform CLI, then rerun the same
command.

## VK-DEP-007

**Cause:** Managed deployment resource identity, ownership, or checkpoint
evidence drifted.

**Fix:** Inspect the platform resources and owner-only resource ledger;
explicitly adopt only verified resources or restore the recorded identity
before retrying.

## VK-DEP-008

**Cause:** A cloud-worker build, runtime bootstrap, or explicit deployment plan
is invalid.

**Fix:** Correct the named runtime, image, region, relay, wheel, project, or
secret input and regenerate the cloud artifacts.

## VK-DEP-009

**Cause:** A required Pipecat Cloud or LiveKit Cloud CLI is missing,
unauthenticated, timed out, or failed.

**Fix:** Install and authenticate the pinned platform CLI, inspect its direct
output, then rerun the same resumable voicekit command.

## VK-DEP-010

**Cause:** Cloud agent ownership, secret-sync, deployment, or rollback evidence
is unsafe.

**Fix:** Inspect the owner-only cloud ledger and platform agent; adopt the exact
id explicitly or restore the ledgered version before retrying.

## VK-TST-001

**Cause:** A simulated-caller scenario, profile, filter, or test config is
invalid.

**Fix:** Correct the reported `tests/scenarios` source or
`tests/voicekit-test.jsonc` field.

## VK-TST-002

**Cause:** A native runtime evaluator is unavailable or rejected generated test
inputs.

**Fix:** Install the selected runtime extra and use the pinned version shown by
`voicekit doctor`.

## VK-TST-003

**Cause:** A configured test tier, sim-caller model, or judge could not execute.

**Fix:** Start the configured local model or supply the documented cloud/live
prerequisites.

## VK-TST-004

**Cause:** One or more voice-agent scenarios failed a hard or judged assertion.

**Fix:** Review the transcript, cited judge reason, and stability attempts before
retrying.

## VK-CLI-001

**Cause:** A non-interactive command lacks an explicit required choice.

**Fix:** Pass the named flag or rerun in an interactive terminal.

## VK-CLI-002

**Cause:** Guided setup was cancelled, conflicted, or cannot resume safely.

**Fix:** Run `voicekit init --resume` or choose a new empty directory.

## VK-CLI-003

**Cause:** A scaffold or protected environment file could not be written safely.

**Fix:** Check permissions and disk space, then resume `voicekit init`.

## VK-CLI-004

**Cause:** A required provider key is missing, invalid, or unverifiable.

**Fix:** Run the printed `voicekit keys add <provider>` command and validate again.

## VK-CLI-005

**Cause:** A requested runtime, carrier, recipe, deploy target, or extra is
unavailable.

**Fix:** Choose an enabled capability or install the exact extra printed.

## VK-CLI-006

**Cause:** One or more doctor preflight checks failed.

**Fix:** Apply each advice line, then rerun `voicekit doctor`.

## VK-CLI-007

**Cause:** The command cannot run from the current project or lifecycle state.

**Fix:** Run the printed next step or resume the interrupted command.

## VK-CLI-008

**Cause:** A money-spending or live-routing mutation lacks confirmation.

**Fix:** Review it, then rerun interactively or pass `--yes`.

## VK-CLI-009

**Cause:** A CLI operation failed without a safe provider-specific mapping.

**Fix:** Retry with `--verbose`; if it repeats, open the pre-filled issue link.

## VK-CLI-010

**Cause:** A command output request or filter is malformed.

**Fix:** Correct the reported flag, identifier, or output option and retry.
