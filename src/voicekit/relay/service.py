"""FastAPI relay surface and repository-backed protocol execution."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TypeVar, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, JsonValue, ValidationError

from voicekit.errors import VoicekitError
from voicekit.obs.latency import LatencySample
from voicekit.obs.logging import scrub_secrets
from voicekit.obs.records import TimelineEvent, ToolCallObservation, TranscriptTurn
from voicekit.relay.auth import FenceSigner, RelayKeyring, RelayRequestVerifier
from voicekit.relay.journal import SQLiteRelayJournal
from voicekit.relay.models import (
    RelayBeginRequest,
    RelayClaimRequest,
    RelayLeaseResponse,
    RelayReadyResponse,
    RelayUpdateRequest,
    RelayUpdateResponse,
)
from voicekit.storage.models import (
    CallLease,
    RecordingReady,
    ResultSnapshot,
    TerminalRequest,
)
from voicekit.storage.sqlite import SQLiteRepository


class RepositoryRelayBackend:
    """Crash-retry-safe protocol adapter over the durable call repository."""

    def __init__(
        self,
        repository: SQLiteRepository,
        journal: SQLiteRelayJournal,
        *,
        fences: FenceSigner,
    ) -> None:
        self.repository = repository
        self.journal = journal
        self.fences = fences

    async def ready(self) -> bool:
        await self.repository.pragmas()
        return await self.journal.ready()

    async def begin(self, request: RelayBeginRequest, request_hash: str) -> RelayLeaseResponse:
        cached = await self.journal.reserve_request(
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            request_kind="begin",
            call_id=request.call.call_id,
            now=request.requested_at,
        )
        if cached is not None:
            return RelayLeaseResponse.model_validate_json(cached)
        try:
            lease = await self.repository.begin_call(
                request.call,
                owner_id=request.owner_id,
                delivery=request.delivery,
                lease_ttl=timedelta(seconds=request.lease_ttl_s),
                now=request.requested_at,
            )
        except VoicekitError as exc:
            try:
                lease = await self.repository.current_relay_lease(request.call.call_id)
            except VoicekitError:
                raise exc from None
            if lease.owner_id != request.owner_id or lease.generation != 1:
                raise exc
        response = RelayLeaseResponse(
            lease=lease,
            fence_token=self.fences.issue(lease),
            next_sequence=await self.journal.next_sequence(lease.call_id),
        )
        await self.journal.complete_request(
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            response_body=response.model_dump_json().encode(),
        )
        return response

    async def claim(self, request: RelayClaimRequest, request_hash: str) -> RelayLeaseResponse:
        cached = await self.journal.reserve_request(
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            request_kind="claim",
            call_id=request.call_id,
            now=request.requested_at,
        )
        if cached is not None:
            return RelayLeaseResponse.model_validate_json(cached)
        try:
            lease = await self.repository.handoff_call(
                request.call_id,
                expected_owner_id=request.expected_owner_id,
                owner_id=request.owner_id,
                lease_ttl=timedelta(seconds=request.lease_ttl_s),
                now=request.requested_at,
            )
        except VoicekitError as exc:
            current = await self.repository.current_relay_lease(request.call_id)
            if current.owner_id != request.owner_id:
                raise exc
            lease = current
        response = RelayLeaseResponse(
            lease=lease,
            fence_token=self.fences.issue(lease),
            next_sequence=await self.journal.next_sequence(lease.call_id),
        )
        await self.journal.complete_request(
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            response_body=response.model_dump_json().encode(),
        )
        return response

    async def update(
        self,
        call_id: str,
        request: RelayUpdateRequest,
        request_hash: str,
    ) -> RelayUpdateResponse:
        lease = self.fences.verify(request.fence_token, call_id=call_id)
        await self.repository.assert_relay_fence(lease)
        cached = await self.journal.reserve_update(
            call_id=call_id,
            sequence=request.sequence,
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            now=request.requested_at,
        )
        if cached is not None:
            return RelayUpdateResponse.model_validate_json(cached)
        result, next_lease = await self._apply(lease, request)
        response = RelayUpdateResponse(
            sequence=request.sequence,
            next_sequence=request.sequence + 1,
            result=result,
            fence_token=(None if next_lease is None else self.fences.issue(next_lease)),
        )
        await self.journal.complete_update(
            call_id=call_id,
            sequence=request.sequence,
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            response_body=response.model_dump_json().encode(),
        )
        return response

    async def _apply(
        self,
        lease: CallLease,
        request: RelayUpdateRequest,
    ) -> tuple[dict[str, JsonValue], CallLease | None]:
        current = lease
        payload = request.payload
        operation_id = hashlib.sha256(
            f"{current.call_id}\0{request.idempotency_key}".encode()
        ).hexdigest()
        if request.operation == "renew_lease":
            ttl = _positive_seconds(payload, "lease_ttl_s")
            renewed = await self.repository.renew_lease(
                current,
                lease_ttl=timedelta(seconds=ttl),
                now=request.requested_at,
            )
            return cast("dict[str, JsonValue]", {"lease": renewed.model_dump(mode="json")}), renewed
        if request.operation == "append_timeline":
            await self.repository.append_timeline_once(
                current.call_id,
                _model(TimelineEvent, payload),
                operation_id=operation_id,
                owner_id=current.owner_id,
                generation=current.generation,
            )
        elif request.operation == "append_transcript":
            await self.repository.append_transcript_once(
                current.call_id,
                _model(TranscriptTurn, payload),
                operation_id=operation_id,
                owner_id=current.owner_id,
                generation=current.generation,
            )
        elif request.operation == "record_tool_call":
            await self.repository.record_tool_call_once(
                current.call_id,
                _model(ToolCallObservation, payload),
                operation_id=operation_id,
                owner_id=current.owner_id,
                generation=current.generation,
            )
        elif request.operation == "record_latency":
            await self.repository.record_latency_once(
                current.call_id,
                _model(LatencySample, payload),
                operation_id=operation_id,
                owner_id=current.owner_id,
                generation=current.generation,
            )
        elif request.operation == "flush_results":
            await self.repository.flush_results(current, _model(ResultSnapshot, payload))
        elif request.operation == "update_provider_state":
            state = payload.get("state")
            if not isinstance(state, str) or not state:
                raise VoicekitError("VK-REL-001", detail="provider state payload is invalid.")
            await self.repository.update_provider_state(current, state)
        elif request.operation == "terminalize":
            event = await self.repository.terminalize(current, _model(TerminalRequest, payload))
            return cast("dict[str, JsonValue]", {"event": event.model_dump(mode="json")}), current
        elif request.operation == "mark_recording_ready":
            event = await self.repository.mark_recording_ready(
                _model(RecordingReady, payload),
                relay_lease=current,
            )
            return cast("dict[str, JsonValue]", {"event": event.model_dump(mode="json")}), current
        elif request.operation == "mark_recording_failed":
            await self.repository.mark_recording_failed_fenced(current)
        else:
            raise VoicekitError("VK-REL-001", detail="relay operation is unsupported.")
        return {}, None


def create_relay_app(
    backend: RepositoryRelayBackend,
    *,
    keyring: RelayKeyring,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the signed relay app; every route, including readiness, authenticates."""
    current_time = clock or (lambda: datetime.now(UTC))
    verifier = RelayRequestVerifier(
        keyring,
        clock=lambda: current_time().timestamp(),
    )
    app = FastAPI(
        title="voicekit results relay",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    async def authenticate(request: Request) -> bytes:
        body = await request.body()
        key_id, nonce, expires_at = verifier.verify(
            request.headers,
            method=request.method,
            path=request.url.path,
            body=body,
        )
        await backend.journal.claim_nonce(
            key_id=key_id,
            nonce=nonce,
            expires_at=expires_at,
            now=current_time(),
        )
        return body

    @app.exception_handler(VoicekitError)
    async def voicekit_error(_request: Request, exc: VoicekitError) -> JSONResponse:
        status = 503 if exc.code in {"VK-REL-002", "VK-REL-006"} else 409
        if exc.code in {"VK-REL-003", "VK-REL-004"}:
            status = 401
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": exc.code,
                    "detail": str(scrub_secrets(exc.detail or "")),
                }
            },
        )

    @app.get("/v1/ready", response_model=RelayReadyResponse)
    async def ready(request: Request) -> RelayReadyResponse:
        await authenticate(request)
        if not await backend.ready():
            raise VoicekitError("VK-REL-002", detail="relay storage is not ready.")
        return RelayReadyResponse(
            ready=True,
            protocol="voicekit-results-relay/v1",
            storage_ready=True,
        )

    @app.post("/v1/calls/begin", response_model=RelayLeaseResponse)
    async def begin(request: Request) -> RelayLeaseResponse:
        raw = await authenticate(request)
        body = _wire_model(RelayBeginRequest, raw)
        return await backend.begin(body, hashlib.sha256(raw).hexdigest())

    @app.post("/v1/calls/claim", response_model=RelayLeaseResponse)
    async def claim(request: Request) -> RelayLeaseResponse:
        raw = await authenticate(request)
        body = _wire_model(RelayClaimRequest, raw)
        return await backend.claim(body, hashlib.sha256(raw).hexdigest())

    @app.post("/v1/calls/{call_id}/updates", response_model=RelayUpdateResponse)
    async def update(
        call_id: str,
        request: Request,
    ) -> RelayUpdateResponse:
        raw = await authenticate(request)
        body = _wire_model(RelayUpdateRequest, raw)
        return await backend.update(call_id, body, hashlib.sha256(raw).hexdigest())

    @app.get("/v1/calls/{call_id}")
    async def get_call(call_id: str, request: Request) -> dict[str, object]:
        await authenticate(request)
        call = await backend.repository.get_call(call_id)
        return {"call": call.model_dump(mode="json")}

    @app.get("/v1/calls/{call_id}/recording")
    async def get_recording_for_call(call_id: str, request: Request) -> dict[str, object]:
        await authenticate(request)
        recording = await backend.repository.get_recording_for_call(call_id)
        return {"recording": None if recording is None else recording.model_dump(mode="json")}

    @app.get("/v1/recordings/{recording_id}")
    async def get_recording(recording_id: str, request: Request) -> dict[str, object]:
        await authenticate(request)
        recording = await backend.repository.get_recording(recording_id)
        return {"recording": recording.model_dump(mode="json")}

    _ = (
        voicekit_error,
        ready,
        begin,
        claim,
        update,
        get_call,
        get_recording_for_call,
        get_recording,
    )
    return app


def _positive_seconds(payload: dict[str, JsonValue], field: str) -> float:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 3600:
        raise VoicekitError("VK-REL-001", detail=f"{field} is invalid.")
    return float(value)


ModelT = TypeVar("ModelT", bound=BaseModel)


def _model(
    model: type[ModelT],
    payload: dict[str, JsonValue],
) -> ModelT:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise VoicekitError("VK-REL-001", detail="relay operation payload is invalid.") from exc


def _wire_model(
    model: type[ModelT],
    body: bytes,
) -> ModelT:
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        raise VoicekitError("VK-REL-001", detail="relay request body is invalid.") from exc
