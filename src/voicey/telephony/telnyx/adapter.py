"""Certified Telnyx Voice API and TeXML adapter."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from voicey.errors import VoiceyError
from voicey.storage.artifacts import ArtifactStore
from voicey.telephony.ledger import OutboundIntent, RouteSettings, TelephonyLedger
from voicey.telephony.models import (
    CallEvent,
    Capabilities,
    CarrierAccountState,
    LiveKitTarget,
    NumberInfo,
    PipecatTarget,
    RollbackToken,
    RuntimeTarget,
    TelephonyRequest,
    validate_e164,
    validate_identifier,
)

_COUNTRY = re.compile(r"^[A-Z]{2}$")
_AREA = re.compile(r"^[0-9]{1,8}$")
_DTMF = re.compile(r"^[0-9*#]{1,32}$")
_PROVIDER_ID = re.compile(r"^[^\x00-\x20\x7f]{1,512}$")
_TERMINAL_EVENTS = frozenset({"call.hangup"})
_NORMAL_HANGUPS = frozenset(
    {
        "normal_clearing",
        "originator_cancel",
    }
)


class TelnyxAdapter:
    """Telnyx numbers, Voice API commands, TeXML, and signed callbacks."""

    provider = "telnyx"
    capabilities = Capabilities(
        inbound=True,
        outbound=True,
        amd=True,
        dtmf_receive=True,
        dtmf_send=True,
        transfer_modes=frozenset({"cold"}),
        recording=True,
        regions=(
            "Latency",
            "Chicago, IL",
            "Ashburn, VA",
            "San Jose, CA",
            "Sydney, Australia",
            "Amsterdam, Netherlands",
            "London, UK",
            "Toronto, Canada",
            "Vancouver, Canada",
            "Frankfurt, Germany",
        ),
        native_outbound_idempotency=True,
        livekit_sip=True,
    )

    def __init__(
        self,
        *,
        api_key: str | None = None,
        public_key: str | None = None,
        connection_id: str | None = None,
        ledger: TelephonyLedger | None = None,
        ledger_path: Path | None = None,
        client: httpx.Client | None = None,
        recording_client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.telnyx.com/v2",
        replay_tolerance_s: int = 300,
        purchase_timeout_s: float = 60.0,
        purchase_poll_interval_s: float = 1.0,
        clock: Any = time.time,
        monotonic: Any = time.monotonic,
        sleeper: Any = time.sleep,
    ) -> None:
        self._api_key = api_key or os.environ.get("TELNYX_API_KEY", "")
        self.connection_id = connection_id or os.environ.get("TELNYX_CONNECTION_ID", "")
        raw_public_key = public_key or os.environ.get("TELNYX_PUBLIC_KEY", "")
        if not self._api_key:
            raise VoiceyError("VY-TEL-002", detail="TELNYX_API_KEY is required.")
        if self.connection_id:
            _validate_provider_id(self.connection_id, field_name="connection id")
        if replay_tolerance_s < 30 or replay_tolerance_s > 900:
            raise VoiceyError(
                "VY-TEL-002",
                detail="Telnyx replay tolerance must be between 30 and 900 seconds.",
            )
        if not base_url.startswith("https://"):
            raise VoiceyError("VY-TEL-002", detail="Telnyx base URL must be HTTPS.")
        if purchase_timeout_s <= 0 or purchase_poll_interval_s <= 0:
            raise VoiceyError(
                "VY-TEL-002",
                detail="Telnyx number-order polling values must be positive.",
            )
        self._public_key = _parse_public_key(raw_public_key) if raw_public_key else None
        self._ledger = ledger or TelephonyLedger(
            ledger_path or Path(".voicey") / "telephony.sqlite3"
        )
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30,
            follow_redirects=False,
        )
        self._recording_client = recording_client
        self._replay_tolerance_s = replay_tolerance_s
        self._purchase_timeout_s = purchase_timeout_s
        self._purchase_poll_interval_s = purchase_poll_interval_s
        self._clock = clock
        self._monotonic = monotonic
        self._sleeper = sleeper

    @property
    def ledger(self) -> TelephonyLedger:
        """Expose durable routing and intent evidence to recovery commands."""
        return self._ledger

    def list_numbers(self) -> list[NumberInfo]:
        data = self._data_list(
            self._request(
                "GET",
                "/phone_numbers",
                params={"page[size]": "250"},
                expected=(200,),
                operation="list numbers",
            )
        )
        return [_number_info(item) for item in data]

    def account_state(self) -> CarrierAccountState:
        """Return non-secret balance facts used by doctor and live preflight."""
        data = self._data_object(
            self._request(
                "GET",
                "/balance",
                expected=(200,),
                operation="inspect account balance",
            )
        )
        return CarrierAccountState(
            provider=self.provider,
            status="active",
            account_type=None,
            balance=_optional(data.get("balance")),
            currency=_optional(data.get("currency")),
        )

    def buy_number(self, country: str, area: str | None = None) -> NumberInfo:
        normalized_country = country.upper()
        if not _COUNTRY.fullmatch(normalized_country):
            raise VoiceyError("VY-TEL-002", detail="country must be an ISO-3166 alpha-2 code.")
        if area is not None and not _AREA.fullmatch(area):
            raise VoiceyError(
                "VY-TEL-002",
                detail="Telnyx national destination code must contain 1-8 digits.",
            )
        params = {
            "filter[country_code]": normalized_country,
            "filter[features]": "voice",
            "filter[best_effort]": "false",
            "page[size]": "20",
        }
        if area is not None:
            params["filter[national_destination_code]"] = area
        candidates = self._data_list(
            self._request(
                "GET",
                "/available_phone_numbers",
                params=params,
                expected=(200,),
                operation="search available numbers",
            )
        )
        if not candidates:
            raise VoiceyError(
                "VY-TEL-003",
                detail=(
                    f"no Telnyx voice number is available for {normalized_country}/{area or '*'}."
                ),
            )
        number = validate_e164(str(candidates[0].get("phone_number", "")))
        response = self._request(
            "POST",
            "/number_orders",
            json_body={"phone_numbers": [{"phone_number": number}]},
            expected=(200, 201, 202),
            operation="buy number",
        )
        order = self._data_object(response)
        order_id = _validate_provider_id(
            str(order.get("id", "")),
            field_name="number order id",
        )
        deadline = float(self._monotonic()) + self._purchase_timeout_s
        while True:
            try:
                return _number_info(self._owned_number(number))
            except VoiceyError as exc:
                if exc.code != "VY-TEL-003":
                    raise
            if float(self._monotonic()) >= deadline:
                raise VoiceyError(
                    "VY-TEL-011",
                    detail=(
                        f"Telnyx accepted number order {order_id!r}, but ownership is still "
                        "pending; inspect the order before retrying."
                    ),
                )
            self._sleeper(self._purchase_poll_interval_s)

    def release_number(self, number: str) -> None:
        owned = self._owned_number(number)
        self._request(
            "DELETE",
            f"/phone_numbers/{owned['id']}",
            expected=(200, 202, 204),
            operation="release number",
        )

    def inbound_route(self, number: str) -> dict[str, str | None]:
        """Fetch the exact number connection used for conflict-safe restore."""
        owned = self._owned_number(number)
        return {"connection_id": _optional(owned.get("connection_id"))}

    def point_inbound(
        self,
        number: str,
        target: RuntimeTarget,
    ) -> RollbackToken:
        _pipecat_target(target)
        connection_id = self._required_connection_id()
        owned = self._owned_number(number)
        normalized = validate_e164(str(owned.get("phone_number", "")))
        snapshot: RouteSettings = {"connection_id": _optional(owned.get("connection_id"))}
        applied: RouteSettings = {"connection_id": connection_id}
        route = self._ledger.prepare_route(
            provider=self.provider,
            number=normalized,
            number_sid=str(owned["id"]),
            snapshot=snapshot,
            applied=applied,
        )
        try:
            updated = self._data_object(
                self._request(
                    "PATCH",
                    f"/phone_numbers/{owned['id']}",
                    json_body=applied,
                    expected=(200,),
                    operation="point inbound number",
                )
            )
        except VoiceyError as exc:
            state: Literal["failed", "ambiguous"] = (
                "failed" if exc.code == "VY-TEL-004" else "ambiguous"
            )
            self._ledger.transition_route(
                route.token,
                expected=("prepared",),
                state=state,
            )
            if state == "ambiguous":
                raise VoiceyError(
                    "VY-TEL-006",
                    detail=f"Telnyx route outcome is ambiguous for token {route.token!r}.",
                ) from exc
            raise
        if {"connection_id": _optional(updated.get("connection_id"))} != applied:
            self._ledger.transition_route(
                route.token,
                expected=("prepared",),
                state="ambiguous",
            )
            raise VoiceyError(
                "VY-TEL-006",
                detail=f"Telnyx did not confirm the route for token {route.token!r}.",
            )
        self._ledger.transition_route(
            route.token,
            expected=("prepared",),
            state="applied",
        )
        return RollbackToken(provider=self.provider, token=route.token)

    def restore(self, token: RollbackToken) -> None:
        if token.provider != self.provider:
            raise VoiceyError("VY-TEL-006", detail="routing token belongs to another carrier.")
        route = self._ledger.get_route(token.token)
        if route.provider != self.provider:
            raise VoiceyError("VY-TEL-006", detail="routing token belongs to another carrier.")
        if route.state == "restored":
            return
        if route.state not in {"applied", "ambiguous", "prepared"}:
            raise VoiceyError(
                "VY-TEL-006",
                detail=f"routing token {token.token!r} cannot restore from {route.state!r}.",
            )
        current = self._owned_number(route.number_sid)
        current_route = {"connection_id": _optional(current.get("connection_id"))}
        if current_route != route.applied and current_route != route.snapshot:
            self._ledger.transition_route(
                token.token,
                expected=(route.state,),
                state="conflict",
            )
            raise VoiceyError(
                "VY-TEL-006",
                detail=f"Telnyx route changed after token {token.token!r} was applied.",
            )
        if current_route != route.snapshot:
            try:
                restored = self._data_object(
                    self._request(
                        "PATCH",
                        f"/phone_numbers/{route.number_sid}",
                        json_body=route.snapshot,
                        expected=(200,),
                        operation="restore inbound number",
                    )
                )
            except VoiceyError as exc:
                self._ledger.transition_route(
                    token.token,
                    expected=(route.state,),
                    state="conflict",
                )
                raise VoiceyError(
                    "VY-TEL-006",
                    detail=f"Telnyx route restore conflicted for token {token.token!r}.",
                ) from exc
            if {"connection_id": _optional(restored.get("connection_id"))} != route.snapshot:
                self._ledger.transition_route(
                    token.token,
                    expected=(route.state,),
                    state="conflict",
                )
                raise VoiceyError(
                    "VY-TEL-006",
                    detail=f"Telnyx route restore did not compare equal for {token.token!r}.",
                )
        self._ledger.transition_route(
            token.token,
            expected=(route.state,),
            state="restored",
        )

    def restore_open_routes(self) -> int:
        """Restore all interrupted temporary Telnyx routes."""
        routes = self._ledger.open_routes(provider=self.provider)
        for route in routes:
            self.restore(RollbackToken(provider=self.provider, token=route.token))
        return len(routes)

    def start_call(
        self,
        from_no: str,
        to_no: str,
        target: RuntimeTarget,
        *,
        intent_id: str | None = None,
        amd: bool = False,
        record: bool = False,
        timeout_s: int = 30,
    ) -> str:
        pipecat = _pipecat_target(target)
        from_number = validate_e164(from_no)
        to_number = validate_e164(to_no)
        if not 5 <= timeout_s <= 600:
            raise VoiceyError("VY-TEL-002", detail="call timeout must be between 5 and 600s.")
        durable_id = validate_identifier(
            intent_id or f"intent_{uuid.uuid4().hex}",
            field_name="outbound intent id",
        )
        self._ledger.prepare_intent(
            intent_id=durable_id,
            provider=self.provider,
            from_number=from_number,
            to_number=to_number,
            target={
                "https_base": pipecat.https_base,
                "ws_path": pipecat.ws_path,
                "record": record,
                "amd": amd,
            },
        )
        body: dict[str, object] = {
            "connection_id": self._required_connection_id(),
            "from": from_number,
            "to": to_number,
            "command_id": durable_id,
            "client_state": _client_state(durable_id),
            "webhook_url": pipecat.event_url(),
            "webhook_url_method": "POST",
            "timeout_secs": timeout_s,
        }
        if amd:
            body["answering_machine_detection"] = "detect"
        if record:
            body["record"] = "record-from-answer-dual"
            body["recording_channels"] = "dual"
            body["recording_format"] = "mp3"
        try:
            data = self._data_object(
                self._request(
                    "POST",
                    "/calls",
                    json_body=body,
                    expected=(200, 201, 202),
                    operation="start outbound call",
                )
            )
        except VoiceyError as exc:
            if exc.code == "VY-TEL-004":
                self._ledger.transition_intent(
                    durable_id,
                    expected=("prepared",),
                    state="rejected",
                    last_status="http_4xx",
                )
                raise
            self._ledger.transition_intent(
                durable_id,
                expected=("prepared",),
                state="ambiguous",
                last_status="create_outcome_unknown",
            )
            raise VoiceyError(
                "VY-TEL-007",
                detail=f"reconcile Telnyx outbound intent {durable_id!r}; it was not retried.",
            ) from exc
        call_id = _validate_provider_id(
            str(data.get("call_control_id") or data.get("call_leg_id") or ""),
            field_name="call control id",
        )
        try:
            self._ledger.transition_intent(
                durable_id,
                expected=("prepared",),
                state="submitted",
                provider_call_id=call_id,
                last_status="initiated",
            )
        except VoiceyError as exc:
            raise VoiceyError(
                "VY-TEL-007",
                detail=f"Telnyx accepted outbound intent {durable_id!r}; reconcile it.",
            ) from exc
        return call_id

    def reconcile_outbound(self, intent_id: str) -> OutboundIntent:
        """Return reconciliation established by a signed callback's client state."""
        intent = self._ledger.get_intent(intent_id)
        if intent.provider_call_id is not None:
            return intent
        raise VoiceyError(
            "VY-TEL-007",
            detail=(
                f"outbound intent {intent_id!r} has no signed Telnyx callback yet; "
                "do not retry the call."
            ),
        )

    def answer_call(self, call_control_id: str) -> None:
        """Answer one parked Voice API call with a native idempotent command."""
        call_id = _validate_provider_id(call_control_id, field_name="call control id")
        self._call_action(
            call_id,
            "answer",
            {"command_id": _command_id("answer", call_id)},
        )

    def start_media(self, call_control_id: str, target: RuntimeTarget) -> None:
        """Start certified bidirectional raw-RTP-over-JSON media streaming."""
        call_id = _validate_provider_id(call_control_id, field_name="call control id")
        pipecat = _pipecat_target(target)
        self._call_action(
            call_id,
            "streaming_start",
            {
                "command_id": _command_id("stream", call_id),
                "stream_url": pipecat.stream_url,
                "stream_track": "both_tracks",
                "stream_bidirectional_mode": "rtp",
                "stream_bidirectional_codec": "PCMU",
                "stream_bidirectional_sampling_rate": 8000,
            },
        )

    def start_recording(self, call_control_id: str) -> None:
        """Start one native-idempotent dual-channel MP3 recording."""
        call_id = _validate_provider_id(call_control_id, field_name="call control id")
        self._call_action(
            call_id,
            "record_start",
            {
                "format": "mp3",
                "channels": "dual",
                "command_id": _command_id("record", call_id),
            },
        )

    def send_dtmf(self, call_control_id: str, digits: str) -> None:
        self._call_action(
            _validate_provider_id(call_control_id, field_name="call control id"),
            "send_dtmf",
            {"digits": _validate_dtmf(digits)},
        )

    def cold_transfer(self, call_control_id: str, to_number: str) -> None:
        call_id = _validate_provider_id(call_control_id, field_name="call control id")
        self._call_action(
            call_id,
            "transfer",
            {
                "to": validate_e164(to_number),
                "command_id": _command_id("transfer", call_id),
            },
        )

    def hangup(self, call_control_id: str) -> None:
        call_id = _validate_provider_id(call_control_id, field_name="call control id")
        self._call_action(
            call_id,
            "hangup",
            {"command_id": _command_id("hangup", call_id)},
        )

    async def download_recording(
        self,
        recording_url: str,
        *,
        artifact_store: ArtifactStore,
        storage_key: str,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> str:
        """Copy a signed-callback recording URL into engine-owned storage."""
        parsed = urlsplit(recording_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise VoiceyError("VY-TEL-009", detail="Telnyx recording URL is not safe HTTPS.")
        if max_bytes <= 0:
            raise VoiceyError("VY-TEL-009", detail="recording size limit must be positive.")
        client = self._recording_client or httpx.AsyncClient(
            timeout=30,
            follow_redirects=False,
        )
        owns_client = self._recording_client is None
        content = bytearray()
        try:
            async with client.stream("GET", recording_url) as response:
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared is not None and int(declared) > max_bytes:
                    raise VoiceyError("VY-TEL-009", detail="recording exceeds size limit.")
                content_type = response.headers.get("content-type", "").split(";", maxsplit=1)[0]
                if content_type not in {
                    "audio/mpeg",
                    "audio/mp3",
                    "audio/wav",
                    "audio/x-wav",
                    "application/octet-stream",
                }:
                    raise VoiceyError(
                        "VY-TEL-009",
                        detail="recording response has an unexpected content type.",
                    )
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise VoiceyError("VY-TEL-009", detail="recording exceeds size limit.")
            await artifact_store.put(storage_key, bytes(content))
        except VoiceyError:
            raise
        except (ValueError, httpx.HTTPError) as exc:
            raise VoiceyError("VY-TEL-009", detail="recording download failed.") from exc
        finally:
            if owns_client:
                await client.aclose()
        return storage_key

    def verify_request(self, request: TelephonyRequest) -> bool:
        if request.is_websocket or self._public_key is None or request.raw_body is None:
            return False
        timestamp = _header(request.headers, "telnyx-timestamp")
        signature = _header(request.headers, "telnyx-signature-ed25519")
        if not timestamp or not signature:
            return False
        try:
            signed_at = int(timestamp)
            now = int(self._clock())
            if abs(now - signed_at) > self._replay_tolerance_s:
                return False
            decoded = base64.b64decode(signature, validate=True)
            message = f"{timestamp}|{request.raw_body}".encode()
            self._public_key.verify(decoded, message)
        except (InvalidSignature, ValueError, TypeError, binascii.Error):
            return False
        return True

    def answer_response(self, target: RuntimeTarget) -> str:
        """Return native TeXML with the same certified bidirectional media contract."""
        pipecat = _pipecat_target(target)
        response = ET.Element("Response")
        connect = ET.SubElement(response, "Connect")
        stream = ET.SubElement(
            connect,
            "Stream",
            {
                "url": pipecat.stream_url,
                "track": "both_tracks",
                "codec": "PCMU",
                "bidirectionalMode": "rtp",
                "bidirectionalCodec": "PCMU",
                "bidirectionalSamplingRate": "8000",
                "statusCallback": pipecat.event_url(),
                "statusCallbackMethod": "POST",
            },
        )
        for name, value in sorted(pipecat.custom_parameters.items()):
            ET.SubElement(stream, "Parameter", {"name": name, "value": value})
        encoded = ET.tostring(response, encoding="utf-8", xml_declaration=True).decode()
        if len(encoded.encode()) > 4096:
            raise VoiceyError("VY-TEL-002", detail="inline TeXML exceeds the 4KB limit.")
        return encoded

    def parse_event(self, request: TelephonyRequest) -> CallEvent:
        content_type = _header(request.headers, "content-type").casefold()
        if request.raw_body and "json" in content_type:
            return self._parse_json_event(request)
        if request.form is None and request.raw_body:
            try:
                loaded: object = json.loads(request.raw_body)
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                return self._parse_json_event(request)
        return self._parse_texml_event(request)

    def _parse_json_event(self, request: TelephonyRequest) -> CallEvent:
        try:
            loaded: object = json.loads(request.raw_body or "")
        except json.JSONDecodeError as exc:
            raise VoiceyError("VY-TEL-008", detail="Telnyx callback is not valid JSON.") from exc
        if not isinstance(loaded, dict):
            raise VoiceyError("VY-TEL-008", detail="Telnyx callback envelope is not an object.")
        document = cast("dict[str, object]", loaded)
        raw_data = document.get("data", document)
        if not isinstance(raw_data, dict):
            raise VoiceyError("VY-TEL-008", detail="Telnyx callback lacks data.")
        data = cast("dict[str, object]", raw_data)
        raw_payload = data.get("payload")
        if not isinstance(raw_payload, dict):
            raise VoiceyError("VY-TEL-008", detail="Telnyx callback lacks payload.")
        payload = cast("dict[str, object]", raw_payload)
        event_type = str(data.get("event_type", ""))
        call_id = _validate_provider_id(
            str(payload.get("call_control_id") or payload.get("call_leg_id") or ""),
            field_name="callback call id",
        )
        intent_id = _intent_from_payload(payload)
        direction = _direction(payload.get("direction"))
        from_number = _optional(payload.get("from"))
        to_number = _optional(payload.get("to"))

        if event_type == "call.dtmf.received":
            return CallEvent(
                type="dtmf",
                provider_call_id=call_id,
                provider_status=event_type,
                digits=_validate_dtmf(str(payload.get("digit", ""))),
                intent_id=intent_id,
                direction=direction,
                from_number=from_number,
                to_number=to_number,
            )
        if event_type == "call.machine.detection.ended":
            return CallEvent(
                type="amd",
                provider_call_id=call_id,
                provider_status=event_type,
                answered_by=str(payload.get("result", "")) or None,
                intent_id=intent_id,
                direction=direction,
                from_number=from_number,
                to_number=to_number,
            )
        if event_type == "call.recording.saved":
            recording_id = _validate_provider_id(
                str(payload.get("recording_id") or payload.get("id") or ""),
                field_name="recording id",
            )
            urls = payload.get("recording_urls")
            recording_url = None
            if isinstance(urls, dict):
                url_values = cast("dict[object, object]", urls)
                recording_url = _optional(url_values.get("mp3") or url_values.get("wav"))
            return CallEvent(
                type="recording_ready",
                provider_call_id=call_id,
                provider_status=event_type,
                recording_sid=recording_id,
                recording_url=recording_url,
                intent_id=intent_id,
                direction=direction,
                from_number=from_number,
                to_number=to_number,
            )
        if event_type == "call.recording.failed":
            return CallEvent(
                type="recording_failed",
                provider_call_id=call_id,
                provider_status=event_type,
                intent_id=intent_id,
                direction=direction,
                from_number=from_number,
                to_number=to_number,
            )

        mapped, reason = _json_status_event(event_type, payload)
        event = CallEvent(
            type=mapped,
            provider_call_id=call_id,
            provider_status=event_type,
            ended_reason=reason,
            intent_id=intent_id,
            direction=direction,
            from_number=from_number,
            to_number=to_number,
        )
        if intent_id is not None:
            self._ledger.bind_callback(
                intent_id,
                provider_call_id=call_id,
                provider_status=event_type,
                terminal=event_type in _TERMINAL_EVENTS,
            )
        return event

    def _parse_texml_event(self, request: TelephonyRequest) -> CallEvent:
        call_id = _validate_provider_id(
            _form_value(request.form, "CallControlId", required=False)
            or _form_value(request.form, "CallSid"),
            field_name="TeXML call id",
        )
        digits = _form_value(request.form, "Digits", required=False)
        if digits:
            return CallEvent(
                type="dtmf",
                provider_call_id=call_id,
                provider_status="dtmf",
                digits=_validate_dtmf(digits),
            )
        status = _form_value(request.form, "CallStatus")
        mapped, reason = _texml_status_event(status)
        return CallEvent(
            type=mapped,
            provider_call_id=call_id,
            provider_status=status,
            ended_reason=reason,
        )

    def _call_action(
        self,
        call_control_id: str,
        action: str,
        body: dict[str, object],
    ) -> None:
        self._request(
            "POST",
            f"/calls/{call_control_id}/actions/{action}",
            json_body=body,
            expected=(200, 202),
            operation=f"{action.replace('_', ' ')} call",
        )

    def _owned_number(self, number_or_id: str) -> dict[str, object]:
        if number_or_id.startswith("+"):
            number = validate_e164(number_or_id)
            response = self._request(
                "GET",
                "/phone_numbers",
                params={"filter[phone_number]": number, "page[size]": "2"},
                expected=(200,),
                operation="find number",
            )
            matches = [
                item
                for item in self._data_list(response)
                if str(item.get("phone_number", "")) == number
            ]
            if len(matches) != 1:
                raise VoiceyError(
                    "VY-TEL-003",
                    detail=f"Telnyx number lookup returned {len(matches)} exact matches.",
                )
            return matches[0]
        provider_id = _validate_provider_id(number_or_id, field_name="phone number id")
        return self._data_object(
            self._request(
                "GET",
                f"/phone_numbers/{provider_id}",
                expected=(200,),
                operation="fetch number",
            )
        )

    def _required_connection_id(self) -> str:
        if not self.connection_id:
            raise VoiceyError(
                "VY-TEL-002",
                detail="TELNYX_CONNECTION_ID is required for Voice API calls and routing.",
            )
        return self.connection_id

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...],
        operation: str,
        params: dict[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        try:
            response = self._client.request(
                method,
                path,
                params=params,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise VoiceyError(
                "VY-TEL-011",
                detail=f"Telnyx {operation} did not return a definitive result.",
            ) from exc
        if response.status_code not in expected:
            if 400 <= response.status_code < 500:
                raise VoiceyError(
                    "VY-TEL-004",
                    detail=f"Telnyx {operation} http_{response.status_code}.",
                )
            raise VoiceyError(
                "VY-TEL-011",
                detail=f"Telnyx {operation} did not return a definitive result.",
            )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            document = response.json()
        except ValueError as exc:
            raise VoiceyError(
                "VY-TEL-011",
                detail=f"Telnyx {operation} returned invalid JSON.",
            ) from exc
        if not isinstance(document, dict):
            raise VoiceyError(
                "VY-TEL-011",
                detail=f"Telnyx {operation} returned an invalid envelope.",
            )
        return cast("dict[str, object]", document)

    @staticmethod
    def _data_object(document: dict[str, object]) -> dict[str, object]:
        data = document.get("data")
        if not isinstance(data, dict):
            raise VoiceyError("VY-TEL-011", detail="Telnyx response lacks an object data field.")
        return cast("dict[str, object]", data)

    @staticmethod
    def _data_list(document: dict[str, object]) -> list[dict[str, object]]:
        data = document.get("data")
        if not isinstance(data, list):
            raise VoiceyError("VY-TEL-011", detail="Telnyx response lacks an array data field.")
        raw_items = cast("list[object]", data)
        if any(not isinstance(item, dict) for item in raw_items):
            raise VoiceyError("VY-TEL-011", detail="Telnyx response has a malformed data item.")
        return cast("list[dict[str, object]]", raw_items)


def _number_info(item: dict[str, object]) -> NumberInfo:
    raw_features = item.get("features")
    capabilities: set[str] = set()
    if isinstance(raw_features, list):
        capabilities.update(str(value) for value in cast("list[object]", raw_features))
    elif isinstance(raw_features, dict):
        capabilities.update(
            str(name)
            for name, enabled in cast("dict[object, object]", raw_features).items()
            if enabled
        )
    return NumberInfo(
        number=validate_e164(str(item.get("phone_number", ""))),
        provider_id=_validate_provider_id(str(item.get("id", "")), field_name="phone number id"),
        friendly_name=_optional(item.get("connection_name")),
        country=_optional(item.get("country_iso_alpha2")),
        locality=_optional(item.get("locality")),
        region=_optional(item.get("administrative_area")),
        capabilities=frozenset(capabilities),
    )


def _parse_public_key(value: str) -> Ed25519PublicKey:
    try:
        stripped = value.strip()
        raw = (
            bytes.fromhex(stripped)
            if re.fullmatch(r"[0-9a-fA-F]{64}", stripped)
            else base64.b64decode(stripped, validate=True)
        )
        if len(raw) != 32:
            raise ValueError("wrong Ed25519 public-key length")
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, binascii.Error) as exc:
        raise VoiceyError(
            "VY-TEL-002",
            detail="TELNYX_PUBLIC_KEY must be a 32-byte hex or base64 Ed25519 key.",
        ) from exc


def _json_status_event(
    event_type: str,
    payload: dict[str, object],
) -> tuple[
    Literal["initiated", "ringing", "answered", "completed", "failed"],
    Literal["provider_hangup", "carrier_error"] | None,
]:
    if event_type == "call.initiated":
        return "initiated", None
    if event_type == "call.ringing":
        return "ringing", None
    if event_type in {"call.answered", "call.bridged", "streaming.started"}:
        return "answered", None
    if event_type == "call.hangup":
        cause = str(payload.get("hangup_cause", ""))
        if cause in _NORMAL_HANGUPS:
            return "completed", "provider_hangup"
        return "failed", "carrier_error"
    if event_type in {"streaming.failed", "call.siprec.failed"}:
        return "failed", "carrier_error"
    raise VoiceyError("VY-TEL-008", detail=f"unknown Telnyx event type {event_type!r}.")


def _texml_status_event(
    status: str,
) -> tuple[
    Literal["initiated", "ringing", "answered", "completed", "failed"],
    Literal["provider_hangup", "carrier_error"] | None,
]:
    if status in {"queued", "initiated"}:
        return "initiated", None
    if status == "ringing":
        return "ringing", None
    if status in {"answered", "in-progress"}:
        return "answered", None
    if status == "completed":
        return "completed", "provider_hangup"
    if status in {"busy", "no-answer", "failed", "canceled"}:
        return "failed", "carrier_error"
    raise VoiceyError("VY-TEL-008", detail=f"unknown Telnyx CallStatus {status!r}.")


def _intent_from_payload(payload: dict[str, object]) -> str | None:
    encoded = payload.get("client_state")
    if encoded in {None, ""}:
        return None
    try:
        raw = base64.b64decode(str(encoded), validate=True)
        loaded: object = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError("client state is not an object")
        document = cast("dict[str, object]", loaded)
        intent_id = str(document.get("intent_id", ""))
        return validate_identifier(intent_id, field_name="outbound intent id")
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise VoiceyError("VY-TEL-008", detail="Telnyx client_state is invalid.") from exc


def _direction(value: object) -> Literal["inbound", "outbound"] | None:
    if value in {None, ""}:
        return None
    normalized = str(value).casefold()
    if normalized in {"incoming", "inbound"}:
        return "inbound"
    if normalized in {"outgoing", "outbound"}:
        return "outbound"
    raise VoiceyError("VY-TEL-008", detail="Telnyx callback direction is invalid.")


def _client_state(intent_id: str) -> str:
    wire = json.dumps(
        {"intent_id": intent_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.b64encode(wire).decode()


def _command_id(action: str, call_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"voicey:telnyx:{action}:{call_id}"))


def _validate_provider_id(value: str, *, field_name: str) -> str:
    if not _PROVIDER_ID.fullmatch(value):
        raise VoiceyError("VY-TEL-008", detail=f"invalid Telnyx {field_name}.")
    return value


def _validate_dtmf(value: str) -> str:
    if not _DTMF.fullmatch(value):
        raise VoiceyError("VY-TEL-002", detail="DTMF digits contain unsupported characters.")
    return value


def _pipecat_target(target: RuntimeTarget) -> PipecatTarget:
    if isinstance(target, LiveKitTarget):
        raise VoiceyError(
            "VY-TEL-002",
            detail="LiveKit targets must use the ledgered TelnyxLiveKitSipProvisioner.",
        )
    return target


def _form_value(form: object | None, name: str, *, required: bool = True) -> str:
    value: object | None = None
    getter = getattr(form, "get", None)
    if callable(getter):
        value = getter(name)
    if value is None or value == "":
        if required:
            raise VoiceyError("VY-TEL-008", detail=f"carrier event lacks {name}.")
        return ""
    return str(value)


def _header(headers: dict[str, str], name: str) -> str:
    normalized = name.casefold()
    return next(
        (value for key, value in headers.items() if key.casefold() == normalized),
        "",
    )


def _optional(value: object) -> str | None:
    return None if value in {None, ""} else str(value)
