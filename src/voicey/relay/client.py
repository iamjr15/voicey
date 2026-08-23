"""Fail-closed cloud-worker client for the results relay."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self, cast
from urllib.parse import quote, urlsplit

import httpx
from pydantic import JsonValue, ValidationError

from voicey.errors import VoiceyError
from voicey.obs.latency import LatencySample
from voicey.obs.records import (
    CallRecord,
    NewCall,
    TimelineEvent,
    ToolCallObservation,
    TranscriptTurn,
)
from voicey.relay.auth import RelayCredential, RelayRequestSigner
from voicey.relay.models import (
    RelayBeginRequest,
    RelayClaimRequest,
    RelayLeaseResponse,
    RelayReadyResponse,
    RelayUpdateRequest,
    RelayUpdateResponse,
)
from voicey.storage.models import (
    CallLease,
    PersistedEvent,
    RecordingReady,
    RecordingSnapshot,
    ResultDeliveryConfig,
    ResultSnapshot,
    TerminalRequest,
)


@dataclass(slots=True)
class _CallState:
    lease: CallLease
    fence_token: str
    next_sequence: int
    pending: RelayUpdateRequest | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RelayClient:
    """Native repository writes over a signed, ordered, idempotent HTTP stream."""

    def __init__(
        self,
        base_url: str,
        credential: RelayCredential,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 10,
        max_attempts: int = 3,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            parsed.scheme not in ({"http", "https"} if loopback else {"https"})
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or timeout_s <= 0
            or not 1 <= max_attempts <= 5
        ):
            raise VoiceyError(
                "VY-REL-001",
                detail="relay client URL or retry setting is invalid.",
            )
        self.base_url = base_url.rstrip("/")
        self._signer = RelayRequestSigner(credential)
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None
        self._max_attempts = max_attempts
        self._states: dict[str, _CallState] = {}
        self._opened = False

    async def __aenter__(self) -> Self:
        return await self.open()

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def open(self) -> Self:
        """Require authenticated durable readiness before worker admission."""
        payload = await self._request("GET", "/v1/ready")
        try:
            ready = RelayReadyResponse.model_validate(payload)
        except ValidationError as exc:
            raise VoiceyError("VY-REL-002", detail="relay readiness payload is invalid.") from exc
        if not ready.ready or not ready.storage_ready:
            raise VoiceyError("VY-REL-002", detail="relay storage is not ready.")
        self._opened = True
        return self

    async def close(self) -> None:
        self._opened = False
        if self._owns_client:
            await self._client.aclose()

    async def begin_call(
        self,
        call: NewCall,
        *,
        owner_id: str,
        delivery: ResultDeliveryConfig,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> CallLease:
        self._require_open()
        request = RelayBeginRequest(
            idempotency_key=_idempotency_key(),
            call=call,
            owner_id=owner_id,
            delivery=delivery,
            lease_ttl_s=lease_ttl.total_seconds(),
            requested_at=now or datetime.now(UTC),
        )
        response = await self._lease_request("/v1/calls/begin", request)
        self._states[call.call_id] = _CallState(
            lease=response.lease,
            fence_token=response.fence_token,
            next_sequence=response.next_sequence,
        )
        return response.lease

    async def handoff_call(
        self,
        call_id: str,
        *,
        expected_owner_id: str,
        owner_id: str,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> CallLease:
        self._require_open()
        request = RelayClaimRequest(
            idempotency_key=_idempotency_key(),
            call_id=call_id,
            expected_owner_id=expected_owner_id,
            owner_id=owner_id,
            lease_ttl_s=lease_ttl.total_seconds(),
            requested_at=now or datetime.now(UTC),
        )
        response = await self._lease_request("/v1/calls/claim", request)
        self._states[call_id] = _CallState(
            lease=response.lease,
            fence_token=response.fence_token,
            next_sequence=response.next_sequence,
        )
        return response.lease

    async def renew_lease(
        self,
        lease: CallLease,
        *,
        lease_ttl: timedelta,
        now: datetime | None = None,
    ) -> CallLease:
        response = await self._update(
            lease.call_id,
            "renew_lease",
            {"lease_ttl_s": lease_ttl.total_seconds()},
            requested_at=now,
        )
        value = response.result.get("lease")
        if not isinstance(value, dict):
            raise VoiceyError("VY-REL-006", detail="relay renewal acknowledgement is invalid.")
        try:
            renewed = CallLease.model_validate(value)
        except ValidationError as exc:
            raise VoiceyError("VY-REL-006", detail="relay renewal lease is invalid.") from exc
        self._states[lease.call_id].lease = renewed
        return renewed

    async def append_timeline(self, call_id: str, event: TimelineEvent) -> None:
        await self._update(call_id, "append_timeline", event.model_dump(mode="json"))

    async def append_transcript(self, call_id: str, turn: TranscriptTurn) -> None:
        await self._update(call_id, "append_transcript", turn.model_dump(mode="json"))

    async def record_tool_call(
        self,
        call_id: str,
        observation: ToolCallObservation,
    ) -> None:
        await self._update(call_id, "record_tool_call", observation.model_dump(mode="json"))

    async def record_latency(self, call_id: str, sample: LatencySample) -> None:
        await self._update(call_id, "record_latency", sample.model_dump(mode="json"))

    async def flush_results(self, lease: CallLease, snapshot: ResultSnapshot) -> None:
        await self._update(lease.call_id, "flush_results", snapshot.model_dump(mode="json"))

    async def update_provider_state(self, lease: CallLease, state: str) -> None:
        await self._update(lease.call_id, "update_provider_state", {"state": state})

    async def terminalize(
        self,
        lease: CallLease,
        request: TerminalRequest,
    ) -> PersistedEvent:
        response = await self._update(
            lease.call_id,
            "terminalize",
            request.model_dump(mode="json"),
        )
        return _event_result(response)

    async def mark_recording_ready(self, update: RecordingReady) -> PersistedEvent:
        recording = await self.get_recording(update.recording_id)
        response = await self._update(
            recording.call_id,
            "mark_recording_ready",
            update.model_dump(mode="json"),
        )
        return _event_result(response)

    async def mark_recording_failed(self, call_id: str) -> None:
        await self._update(call_id, "mark_recording_failed", {})

    async def get_call(self, call_id: str) -> CallRecord:
        payload = await self._request("GET", f"/v1/calls/{quote(call_id, safe='')}")
        try:
            return CallRecord.model_validate(payload["call"])
        except (KeyError, ValidationError) as exc:
            raise VoiceyError("VY-REL-006", detail="relay call response is invalid.") from exc

    async def get_recording_for_call(self, call_id: str) -> RecordingSnapshot | None:
        payload = await self._request(
            "GET",
            f"/v1/calls/{quote(call_id, safe='')}/recording",
        )
        value = payload.get("recording")
        if value is None:
            return None
        try:
            return RecordingSnapshot.model_validate(value)
        except ValidationError as exc:
            raise VoiceyError(
                "VY-REL-006",
                detail="relay recording response is invalid.",
            ) from exc

    async def get_recording(self, recording_id: str) -> RecordingSnapshot:
        payload = await self._request(
            "GET",
            f"/v1/recordings/{quote(recording_id, safe='')}",
        )
        try:
            return RecordingSnapshot.model_validate(payload["recording"])
        except (KeyError, ValidationError) as exc:
            raise VoiceyError(
                "VY-REL-006",
                detail="relay recording response is invalid.",
            ) from exc

    async def _lease_request(
        self,
        path: str,
        request: RelayBeginRequest | RelayClaimRequest,
    ) -> RelayLeaseResponse:
        payload = await self._request("POST", path, body=request.model_dump_json().encode())
        try:
            return RelayLeaseResponse.model_validate(payload)
        except ValidationError as exc:
            raise VoiceyError(
                "VY-REL-006",
                detail="relay lease acknowledgement is invalid.",
            ) from exc

    async def _update(
        self,
        call_id: str,
        operation: str,
        payload: dict[str, JsonValue],
        *,
        requested_at: datetime | None = None,
    ) -> RelayUpdateResponse:
        self._require_open()
        try:
            state = self._states[call_id]
        except KeyError as exc:
            raise VoiceyError(
                "VY-REL-004",
                detail=f"call {call_id!r} has no server-issued fence in this worker.",
            ) from exc
        async with state.lock:
            if state.pending is not None:
                pending = state.pending
                same_operation = pending.operation == operation and pending.payload == payload
                response = await self._send_update(call_id, state, pending)
                if same_operation:
                    return response
            request = RelayUpdateRequest.model_validate(
                {
                    "sequence": state.next_sequence,
                    "idempotency_key": _idempotency_key(),
                    "fence_token": state.fence_token,
                    "operation": operation,
                    "payload": payload,
                    "requested_at": requested_at or datetime.now(UTC),
                }
            )
            state.pending = request
            return await self._send_update(call_id, state, request)

    async def _send_update(
        self,
        call_id: str,
        state: _CallState,
        request: RelayUpdateRequest,
    ) -> RelayUpdateResponse:
        """Send or replay one exact stream operation until its acknowledgement is known."""
        response_payload = await self._request(
            "POST",
            f"/v1/calls/{quote(call_id, safe='')}/updates",
            body=request.model_dump_json().encode(),
        )
        try:
            response = RelayUpdateResponse.model_validate(response_payload)
        except ValidationError as exc:
            raise VoiceyError(
                "VY-REL-006",
                detail="relay update acknowledgement is invalid.",
            ) from exc
        if response.sequence != state.next_sequence:
            raise VoiceyError("VY-REL-005", detail="relay acknowledged a different sequence.")
        state.next_sequence = response.next_sequence
        if response.fence_token is not None:
            state.fence_token = response.fence_token
        state.pending = None
        return response

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
    ) -> dict[str, object]:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                response = await self._client.request(
                    method,
                    f"{self.base_url}{path}",
                    content=body,
                    headers=self._signer.headers(method, path, body),
                )
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code < 400:
                    try:
                        value: object = response.json()
                    except json.JSONDecodeError as exc:
                        raise VoiceyError(
                            "VY-REL-006",
                            detail="relay response is not JSON.",
                        ) from exc
                    if isinstance(value, dict):
                        return cast("dict[str, object]", value)
                    raise VoiceyError("VY-REL-006", detail="relay response is not an object.")
                error = _response_error(response)
                if response.status_code < 500 or attempt + 1 == self._max_attempts:
                    raise error
                last_error = error
            if attempt + 1 < self._max_attempts:
                await asyncio.sleep(0.05 * (attempt + 1))
        raise VoiceyError(
            "VY-REL-002",
            detail="relay request attempts were exhausted.",
        ) from last_error

    def _require_open(self) -> None:
        if not self._opened:
            raise VoiceyError(
                "VY-REL-002",
                detail="relay readiness was not acknowledged; call open() before admission.",
            )


def _idempotency_key() -> str:
    return f"op_{uuid.uuid4().hex}"


def _event_result(response: RelayUpdateResponse) -> PersistedEvent:
    value = response.result.get("event")
    if not isinstance(value, dict):
        raise VoiceyError("VY-REL-006", detail="relay event acknowledgement is invalid.")
    try:
        return PersistedEvent.model_validate(value)
    except ValidationError as exc:
        raise VoiceyError("VY-REL-006", detail="relay event payload is invalid.") from exc


def _response_error(response: httpx.Response) -> VoiceyError:
    try:
        payload: object = response.json()
        if isinstance(payload, dict):
            response_payload = cast("dict[str, object]", payload)
            error = response_payload.get("error")
            if isinstance(error, dict):
                error_payload = cast("dict[str, object]", error)
                code = error_payload.get("code")
                detail = error_payload.get("detail")
                if isinstance(code, str) and isinstance(detail, str):
                    return VoiceyError(code, detail=detail)
    except (json.JSONDecodeError, AssertionError):
        pass
    return VoiceyError(
        "VY-REL-002" if response.status_code >= 500 else "VY-REL-006",
        detail=f"relay returned HTTP {response.status_code}.",
    )
