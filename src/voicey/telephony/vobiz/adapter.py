"""Certified Vobiz Voice API, VobizXML, and signed-callback adapter."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote, urlsplit

import httpx

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

_ACCOUNT_ID = re.compile(r"^(?:MA|SA)_[A-Za-z0-9]{4,128}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_AREA = re.compile(r"^[0-9]{1,8}$")
_DTMF = re.compile(r"^[0-9*#wW]{1,64}$")
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9:_-]{1,256}$")
_NONCE = re.compile(r"^[0-9]{20}$")
_NORMAL_HANGUPS = frozenset(
    {
        "normal_clearing",
        "normal clearing",
        "originator_cancel",
        "end of xml instructions",
        "4010",
        "16",
    }
)


class VobizAdapter:
    """Vobiz numbers, calls, VobizXML, signed callbacks, and media control."""

    provider = "vobiz"
    capabilities = Capabilities(
        inbound=True,
        outbound=True,
        amd=True,
        dtmf_receive=True,
        dtmf_send=True,
        transfer_modes=frozenset({"cold"}),
        recording=True,
        regions=("India", "Global"),
        native_outbound_idempotency=False,
        livekit_sip=True,
    )

    def __init__(
        self,
        *,
        auth_id: str | None = None,
        auth_token: str | None = None,
        ledger: TelephonyLedger | None = None,
        ledger_path: Path | None = None,
        client: httpx.Client | None = None,
        recording_client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.vobiz.ai",
        expected_public_base: str | None = None,
        currency: str = "INR",
        replay_ttl_s: int = 600,
        clock: Any = time.monotonic,
    ) -> None:
        self.auth_id = auth_id or os.environ.get("VOBIZ_AUTH_ID", "")
        self._auth_token = auth_token or os.environ.get("VOBIZ_AUTH_TOKEN", "")
        if not _ACCOUNT_ID.fullmatch(self.auth_id):
            raise VoiceyError("VY-TEL-002", detail="VOBIZ_AUTH_ID is missing or invalid.")
        if not self._auth_token:
            raise VoiceyError("VY-TEL-002", detail="VOBIZ_AUTH_TOKEN is required.")
        parsed_base = urlsplit(base_url)
        if (
            parsed_base.scheme != "https"
            or not parsed_base.hostname
            or parsed_base.username
            or parsed_base.password
            or parsed_base.query
            or parsed_base.fragment
        ):
            raise VoiceyError("VY-TEL-002", detail="Vobiz base URL must be normalized HTTPS.")
        if not re.fullmatch(r"^[A-Z]{3}$", currency):
            raise VoiceyError("VY-TEL-002", detail="Vobiz currency must be a 3-letter code.")
        if not 60 <= replay_ttl_s <= 3600:
            raise VoiceyError(
                "VY-TEL-002",
                detail="Vobiz callback replay TTL must be between 60 and 3600 seconds.",
            )
        self._expected_public_base = _validated_public_base(expected_public_base)
        self._currency = currency
        self._ledger = ledger or TelephonyLedger(
            ledger_path or Path(".voicey") / "telephony.sqlite3"
        )
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "X-Auth-ID": self.auth_id,
                "X-Auth-Token": self._auth_token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30,
            follow_redirects=False,
        )
        self._recording_client = recording_client
        self._replay_ttl_s = replay_ttl_s
        self._clock = clock
        self._seen_nonces: dict[str, float] = {}
        self._nonce_lock = threading.Lock()

    @property
    def ledger(self) -> TelephonyLedger:
        """Expose durable route and outbound-intent evidence."""
        return self._ledger

    def list_numbers(self) -> list[NumberInfo]:
        """List every owned number, following Vobiz page metadata."""
        return [_number_info(item) for item in self._number_pages("/numbers")]

    def account_state(self) -> CarrierAccountState:
        data = self._request(
            "GET",
            f"/api/v1/Account/{quote(self.auth_id, safe='')}/balance/{self._currency}",
            expected=(200,),
            operation="inspect account balance",
        )
        return CarrierAccountState(
            provider=self.provider,
            status=str(data.get("status", "active")),
            account_type="subaccount" if self.auth_id.startswith("SA_") else "master",
            balance=_optional(data.get("available_balance", data.get("balance"))),
            currency=_optional(data.get("currency")) or self._currency,
        )

    def buy_number(self, country: str, area: str | None = None) -> NumberInfo:
        normalized_country = country.upper()
        if not _COUNTRY.fullmatch(normalized_country):
            raise VoiceyError("VY-TEL-002", detail="country must be an ISO-3166 alpha-2 code.")
        if area is not None and not _AREA.fullmatch(area):
            raise VoiceyError(
                "VY-TEL-002",
                detail="Vobiz number search prefix must contain 1-8 digits.",
            )
        candidates = self._number_pages(
            "/inventory/numbers",
            params={
                "country": normalized_country,
                **({} if area is None else {"search": area}),
            },
            first_page_only=True,
        )
        if not candidates:
            raise VoiceyError(
                "VY-TEL-003",
                detail=f"no Vobiz number is available for {normalized_country}/{area or '*'}.",
            )
        number = validate_e164(str(candidates[0].get("e164", "")))
        response = self._request(
            "POST",
            f"/api/v1/Account/{quote(self.auth_id, safe='')}/numbers/purchase-from-inventory",
            json_body={"e164": number, "currency": self._currency},
            expected=(200,),
            operation="buy number",
        )
        raw = response.get("number")
        if not isinstance(raw, dict):
            raise VoiceyError("VY-TEL-011", detail="Vobiz purchase response lacks a number.")
        return _number_info(cast("dict[str, object]", raw))

    def release_number(self, number: str) -> None:
        """Begin Vobiz's recoverable 24-hour release window."""
        owned = self._owned_number(number)
        self._request(
            "DELETE",
            self._number_path(str(owned["e164"])),
            expected=(200,),
            operation="release number",
        )

    def inbound_route(self, number: str) -> dict[str, str | None]:
        owned = self._owned_number(number)
        return _route_from_number(owned)

    def point_inbound(
        self,
        number: str,
        target: RuntimeTarget,
    ) -> RollbackToken:
        pipecat = _pipecat_target(target)
        owned = self._owned_number(number)
        normalized = validate_e164(str(owned.get("e164", "")))
        app_id, created = self._ensure_application(pipecat)
        snapshot = _route_from_number(owned)
        applied: RouteSettings = {
            "application_id": app_id,
            "trunk_group_id": None,
            "managed_application_id": app_id if created else None,
        }
        route = self._ledger.prepare_route(
            provider=self.provider,
            number=normalized,
            number_sid=str(owned["id"]),
            snapshot=snapshot,
            applied=applied,
        )
        try:
            if snapshot["trunk_group_id"] is not None:
                self._unassign_trunk(normalized)
            self._attach_application(normalized, app_id)
            confirmed = _route_from_number(self._owned_number(normalized))
        except VoiceyError as exc:
            state: Literal["failed", "ambiguous"] = (
                "failed" if exc.code == "VY-TEL-004" else "ambiguous"
            )
            self._ledger.transition_route(route.token, expected=("prepared",), state=state)
            if state == "ambiguous":
                raise VoiceyError(
                    "VY-TEL-006",
                    detail=f"Vobiz route outcome is ambiguous for token {route.token!r}.",
                ) from exc
            raise
        if confirmed != {
            "application_id": app_id,
            "trunk_group_id": None,
        }:
            self._ledger.transition_route(
                route.token,
                expected=("prepared",),
                state="ambiguous",
            )
            raise VoiceyError(
                "VY-TEL-006",
                detail=f"Vobiz did not confirm route token {route.token!r}.",
            )
        self._ledger.transition_route(route.token, expected=("prepared",), state="applied")
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
        current = _route_from_number(self._owned_number(route.number))
        comparable_applied = {
            "application_id": route.applied.get("application_id"),
            "trunk_group_id": route.applied.get("trunk_group_id"),
        }
        if current != route.snapshot and current != comparable_applied:
            self._ledger.transition_route(
                token.token,
                expected=(route.state,),
                state="conflict",
            )
            raise VoiceyError(
                "VY-TEL-006",
                detail=f"Vobiz route changed after token {token.token!r} was applied.",
            )
        try:
            if current != route.snapshot:
                if current["application_id"] is not None:
                    self._detach_application(route.number)
                if route.snapshot["trunk_group_id"] is not None:
                    self._assign_trunk(route.number, route.snapshot["trunk_group_id"])
                elif route.snapshot["application_id"] is not None:
                    self._attach_application(route.number, route.snapshot["application_id"])
                confirmed = _route_from_number(self._owned_number(route.number))
                if confirmed != route.snapshot:
                    raise VoiceyError(
                        "VY-TEL-006",
                        detail=f"Vobiz route restore did not compare equal for {token.token!r}.",
                    )
            managed_app = route.applied.get("managed_application_id")
            if managed_app:
                self._request(
                    "DELETE",
                    self._application_path(managed_app),
                    expected=(200, 204),
                    operation="delete managed application",
                )
        except VoiceyError as exc:
            self._ledger.transition_route(
                token.token,
                expected=(route.state,),
                state="conflict",
            )
            raise VoiceyError(
                "VY-TEL-006",
                detail=f"Vobiz route restore conflicted for token {token.token!r}.",
            ) from exc
        self._ledger.transition_route(
            token.token,
            expected=(route.state,),
            state="restored",
        )

    def restore_open_routes(self) -> int:
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
        send_digits: str | None = None,
        record: bool = False,
        timeout_s: int = 30,
    ) -> str:
        pipecat = _pipecat_target(target)
        from_number = validate_e164(from_no)
        to_number = validate_e164(to_no)
        if not 5 <= timeout_s <= 600:
            raise VoiceyError("VY-TEL-002", detail="call timeout must be between 5 and 600s.")
        digits = None if send_digits is None else _validate_dtmf(send_digits)
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
        answer_url = f"{pipecat.answer_url}/{quote(durable_id, safe='')}"
        event_url = pipecat.event_url(durable_id)
        body: dict[str, object] = {
            "from": from_number,
            "to": to_number,
            "answer_url": answer_url,
            "answer_method": "POST",
            "ring_url": event_url,
            "ring_method": "POST",
            "hangup_url": event_url,
            "hangup_method": "POST",
            "hangup_on_ring": timeout_s,
        }
        if digits is not None:
            body["send_digits"] = digits
        if amd:
            body.update(
                {
                    "machine_detection": "true",
                    "machine_detection_url": (f"{pipecat.amd_url}/{quote(durable_id, safe='')}"),
                    "machine_detection_method": "POST",
                }
            )
        try:
            data = self._request(
                "POST",
                self._call_collection_path,
                json_body=body,
                expected=(200,),
                operation="start outbound call",
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
                detail=f"reconcile Vobiz outbound intent {durable_id!r}; it was not retried.",
            ) from exc
        try:
            call_id = _provider_id(
                str(data.get("request_uuid") or data.get("call_uuid") or ""),
                field_name="call UUID",
            )
        except VoiceyError as exc:
            self._ledger.transition_intent(
                durable_id,
                expected=("prepared",),
                state="ambiguous",
                last_status="malformed_create_response",
            )
            raise VoiceyError(
                "VY-TEL-007",
                detail=f"Vobiz accepted outbound intent {durable_id!r} without a call UUID.",
            ) from exc
        try:
            self._ledger.transition_intent(
                durable_id,
                expected=("prepared",),
                state="submitted",
                provider_call_id=call_id,
                last_status="queued",
            )
        except VoiceyError as exc:
            raise VoiceyError(
                "VY-TEL-007",
                detail=f"Vobiz accepted outbound intent {durable_id!r}; reconcile it.",
            ) from exc
        return call_id

    def reconcile_outbound(self, intent_id: str) -> OutboundIntent:
        intent = self._ledger.get_intent(intent_id)
        if intent.provider_call_id is not None:
            return intent
        raise VoiceyError(
            "VY-TEL-007",
            detail=(
                f"outbound intent {intent_id!r} has no signed Vobiz callback yet; "
                "do not retry the call."
            ),
        )

    def start_recording(self, call_uuid: str, target: RuntimeTarget) -> str:
        call_id = _provider_id(call_uuid, field_name="call UUID")
        pipecat = _pipecat_target(target)
        data = self._request(
            "POST",
            self._recording_path(call_id),
            json_body={
                "time_limit": 86400,
                "file_format": "mp3",
                "record_channel_type": "stereo",
                "callback_url": pipecat.recording_url,
                "callback_method": "POST",
            },
            expected=(200,),
            operation="start recording",
        )
        return _provider_id(
            str(data.get("recording_id", "")),
            field_name="recording id",
        )

    def send_dtmf(self, call_uuid: str, digits: str) -> None:
        call_id = _provider_id(call_uuid, field_name="call UUID")
        self._request(
            "POST",
            f"{self._call_collection_path}{quote(call_id, safe='')}/DTMF/",
            json_body={"digits": _validate_dtmf(digits), "leg": "aleg"},
            expected=(200, 202),
            operation="send DTMF",
        )

    def cold_transfer(self, call_uuid: str, to_number: str) -> None:
        if self._expected_public_base is None:
            raise VoiceyError(
                "VY-TEL-002",
                detail="Vobiz transfer requires expected_public_base.",
            )
        call_id = _provider_id(call_uuid, field_name="call UUID")
        destination = validate_e164(to_number)
        # Keep the validated E.164 `+` literal so Vobiz and Starlette sign the same path.
        transfer_url = f"{self._expected_public_base}/vobiz/transfer/{destination}"
        self._request(
            "POST",
            f"{self._call_collection_path}{quote(call_id, safe='')}/",
            json_body={
                "legs": "aleg",
                "aleg_url": transfer_url,
                "aleg_method": "POST",
            },
            expected=(200,),
            operation="cold transfer",
        )

    def hangup(self, call_uuid: str) -> None:
        call_id = _provider_id(call_uuid, field_name="call UUID")
        self._request(
            "DELETE",
            f"{self._call_collection_path}{quote(call_id, safe='')}/",
            expected=(200, 204),
            operation="hang up call",
        )

    async def download_recording(
        self,
        recording_url: str,
        *,
        artifact_store: ArtifactStore,
        storage_key: str,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> str:
        parsed = urlsplit(recording_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise VoiceyError("VY-TEL-009", detail="Vobiz recording URL is not safe HTTPS.")
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
        """Verify V3/V2 HMAC signatures and reject nonce replay."""
        if request.is_websocket or request.scheme.casefold() != "https":
            return False
        nonce = _header(request.headers, "x-vobiz-signature-v3-nonce")
        signature = _header(request.headers, "x-vobiz-signature-v3")
        version = "v3"
        if not nonce or not signature:
            nonce = _header(request.headers, "x-vobiz-signature-v2-nonce")
            signature = _header(request.headers, "x-vobiz-signature-v2")
            version = "v2"
        if not nonce or not signature or not _NONCE.fullmatch(nonce):
            return False
        canonical_url = self._callback_url(request)
        if canonical_url is None:
            return False
        payload = (
            f"{canonical_url}.{nonce}".encode()
            if version == "v3"
            else f"{canonical_url}{nonce}".encode()
        )
        expected = base64.b64encode(
            hmac.new(self._auth_token.encode(), payload, hashlib.sha256).digest()
        ).decode()
        try:
            supplied = base64.b64decode(signature, validate=True)
            valid = hmac.compare_digest(
                supplied,
                base64.b64decode(expected, validate=True),
            )
        except (ValueError, binascii.Error):
            return False
        if not valid:
            return False
        return self._claim_nonce(nonce)

    def answer_response(self, target: RuntimeTarget) -> str:
        """Return current VobizXML for bidirectional PCMU at 8 kHz."""
        pipecat = _pipecat_target(target)
        response = ET.Element("Response")
        stream = ET.SubElement(
            response,
            "Stream",
            {
                "bidirectional": "true",
                "audioTrack": "inbound",
                "contentType": "audio/x-mulaw;rate=8000",
                "keepCallAlive": "true",
                "maxRetries": "2",
                "statusCallbackUrl": pipecat.event_url(),
            },
        )
        stream.text = pipecat.stream_url
        encoded = ET.tostring(response, encoding="utf-8", xml_declaration=True).decode()
        if len(encoded.encode()) > 4096:
            raise VoiceyError("VY-TEL-002", detail="inline VobizXML exceeds the 4KB limit.")
        return encoded

    def transfer_response(self, to_number: str, *, caller_id: str | None = None) -> str:
        destination = validate_e164(to_number)
        response = ET.Element("Response")
        attributes: dict[str, str] = {}
        if caller_id is not None:
            attributes["callerId"] = validate_e164(caller_id)
        dial = ET.SubElement(response, "Dial", attributes)
        ET.SubElement(dial, "Number").text = destination
        return ET.tostring(response, encoding="utf-8", xml_declaration=True).decode()

    def parse_event(self, request: TelephonyRequest) -> CallEvent:
        call_id = _provider_id(
            _form_value(request.form, "CallUUID", required=False)
            or _form_value(request.form, "RequestUUID"),
            field_name="callback call UUID",
        )
        intent_id = request.route_params.get("intent_id")
        if intent_id is not None:
            intent_id = validate_identifier(intent_id, field_name="outbound intent id")
        event_name = _form_value(request.form, "Event", required=False)
        status = _form_value(request.form, "CallStatus", required=False)
        direction = _direction(_form_value(request.form, "Direction", required=False))
        from_number = _optional(_form_value(request.form, "From", required=False))
        to_number = _optional(_form_value(request.form, "To", required=False))

        recording_url = _form_value(request.form, "record_url", required=False) or _form_value(
            request.form,
            "RecordingUrl",
            required=False,
        )
        recording_id = _form_value(
            request.form,
            "recording_id",
            required=False,
        ) or _form_value(request.form, "RecordingID", required=False)
        if recording_url or recording_id:
            if not recording_url or not recording_id:
                raise VoiceyError("VY-TEL-008", detail="Vobiz recording callback is incomplete.")
            return CallEvent(
                type="recording_ready",
                provider_call_id=call_id,
                provider_status=event_name or "recording",
                recording_sid=_provider_id(recording_id, field_name="recording id"),
                recording_url=recording_url,
                intent_id=intent_id,
                direction=direction,
                from_number=from_number,
                to_number=to_number,
            )
        if event_name.casefold() == "machinedetection":
            machine = _form_value(request.form, "Machine").casefold() == "true"
            return CallEvent(
                type="amd",
                provider_call_id=call_id,
                provider_status=event_name,
                answered_by="machine" if machine else "human",
                intent_id=intent_id,
                direction=direction,
                from_number=from_number,
                to_number=to_number,
            )
        digits = _form_value(request.form, "Digits", required=False)
        if digits:
            return CallEvent(
                type="dtmf",
                provider_call_id=call_id,
                provider_status=event_name or "dtmf",
                digits=_validate_dtmf(digits),
                intent_id=intent_id,
                direction=direction,
                from_number=from_number,
                to_number=to_number,
            )
        mapped, ended_reason = _status_event(event_name, status, request.form)
        event = CallEvent(
            type=mapped,
            provider_call_id=call_id,
            provider_status=event_name or status,
            ended_reason=ended_reason,
            intent_id=intent_id,
            direction=direction,
            from_number=from_number,
            to_number=to_number,
        )
        if intent_id is not None:
            self._ledger.bind_callback(
                intent_id,
                provider_call_id=call_id,
                provider_status=event.provider_status,
                terminal=event.ended_reason is not None,
            )
        return event

    @property
    def _call_collection_path(self) -> str:
        return f"/api/v1/Account/{quote(self.auth_id, safe='')}/Call/"

    def _recording_path(self, call_id: str) -> str:
        return f"{self._call_collection_path}{quote(call_id, safe='')}/Record/"

    def _number_path(self, number: str) -> str:
        return (
            f"/api/v1/Account/{quote(self.auth_id, safe='')}/numbers/"
            f"{quote(validate_e164(number), safe='')}"
        )

    def _application_path(self, app_id: str | None = None) -> str:
        base = f"/api/v1/Account/{quote(self.auth_id, safe='')}/Application/"
        if app_id is None:
            return base
        validated = _provider_id(app_id, field_name="app id")
        return f"{base}{quote(validated, safe='')}/"

    def _owned_number(self, number: str) -> dict[str, object]:
        normalized = validate_e164(number)
        matches = [
            item
            for item in self._number_pages(
                "/numbers",
                params={"search": normalized},
                first_page_only=True,
            )
            if str(item.get("e164", "")) == normalized
            and str(item.get("account_id", "")) == self.auth_id
        ]
        if len(matches) != 1:
            raise VoiceyError(
                "VY-TEL-003",
                detail=f"Vobiz number lookup returned {len(matches)} exact owned matches.",
            )
        return matches[0]

    def _number_pages(
        self,
        suffix: str,
        *,
        params: Mapping[str, str] | None = None,
        first_page_only: bool = False,
    ) -> list[dict[str, object]]:
        page = 1
        items: list[dict[str, object]] = []
        while True:
            response = self._request(
                "GET",
                f"/api/v1/Account/{quote(self.auth_id, safe='')}{suffix}",
                params={**(params or {}), "page": str(page), "per_page": "100"},
                expected=(200,),
                operation="list numbers",
            )
            raw_items = response.get("items")
            if not isinstance(raw_items, list):
                raise VoiceyError("VY-TEL-011", detail="Vobiz number list is malformed.")
            raw_number_items = cast("list[object]", raw_items)
            if any(not isinstance(item, dict) for item in raw_number_items):
                raise VoiceyError("VY-TEL-011", detail="Vobiz number list is malformed.")
            page_items = cast("list[dict[str, object]]", raw_number_items)
            items.extend(page_items)
            total = _integer(response.get("total"), default=len(items))
            if first_page_only or not page_items or len(items) >= total:
                return items
            page += 1

    def _ensure_application(self, target: PipecatTarget) -> tuple[str, bool]:
        fingerprint = hashlib.sha256(
            f"{target.answer_url}|{target.event_url()}".encode()
        ).hexdigest()[:20]
        name = f"voicey-{fingerprint}"
        response = self._request(
            "GET",
            self._application_path(),
            params={"limit": "100", "offset": "0"},
            expected=(200,),
            operation="list applications",
        )
        raw_apps = response.get("objects", response.get("applications", []))
        if not isinstance(raw_apps, list):
            raise VoiceyError("VY-TEL-011", detail="Vobiz application list is malformed.")
        raw_application_items = cast("list[object]", raw_apps)
        if any(not isinstance(item, dict) for item in raw_application_items):
            raise VoiceyError("VY-TEL-011", detail="Vobiz application list is malformed.")
        matches = [
            cast("dict[str, object]", item)
            for item in raw_application_items
            if str(cast("dict[str, object]", item).get("app_name", "")) == name
        ]
        if len(matches) > 1:
            raise VoiceyError("VY-TEL-006", detail="duplicate managed Vobiz applications.")
        if matches:
            app = matches[0]
            if (
                str(app.get("answer_url", "")) != target.answer_url
                or str(app.get("answer_method", "")).upper() != "POST"
                or str(app.get("hangup_url", "")) != target.event_url()
                or str(app.get("hangup_method", "")).upper() != "POST"
            ):
                raise VoiceyError(
                    "VY-TEL-006",
                    detail="managed Vobiz application differs from desired config.",
                )
            return _provider_id(str(app.get("app_id", "")), field_name="app id"), False
        created = self._request(
            "POST",
            self._application_path(),
            json_body={
                "app_name": name,
                "answer_url": target.answer_url,
                "answer_method": "POST",
                "hangup_url": target.event_url(),
                "hangup_method": "POST",
                "default_number_app": False,
            },
            expected=(201,),
            operation="create application",
        )
        return _provider_id(str(created.get("app_id", "")), field_name="app id"), True

    def _attach_application(self, number: str, app_id: str | None) -> None:
        if app_id is None:
            raise VoiceyError("VY-TEL-006", detail="Vobiz application id is absent.")
        self._request(
            "POST",
            f"{self._number_path(number)}/application",
            json_body={"application_id": app_id},
            expected=(200,),
            operation="attach number to application",
        )

    def _detach_application(self, number: str) -> None:
        self._request(
            "DELETE",
            f"{self._number_path(number)}/application",
            expected=(200, 204),
            operation="detach number from application",
        )

    def _assign_trunk(self, number: str, trunk_id: str | None) -> None:
        if trunk_id is None:
            raise VoiceyError("VY-TEL-006", detail="Vobiz trunk id is absent.")
        self._request(
            "POST",
            f"{self._number_path(number)}/assign",
            json_body={"trunk_group_id": trunk_id},
            expected=(204,),
            operation="assign number to trunk",
        )

    def _unassign_trunk(self, number: str) -> None:
        self._request(
            "DELETE",
            f"{self._number_path(number)}/assign",
            expected=(204,),
            operation="unassign number from trunk",
        )

    def _callback_url(self, request: TelephonyRequest) -> str | None:
        if (
            not request.host
            or "@" in request.host
            or not request.path.startswith("/")
            or "#" in request.path
            or "?" in request.path
        ):
            return None
        if self._expected_public_base is None:
            return f"https://{request.host}{request.path}"
        expected = urlsplit(self._expected_public_base)
        if request.host.casefold() != expected.netloc.casefold():
            return None
        return f"{self._expected_public_base}{request.path}"

    def _claim_nonce(self, nonce: str) -> bool:
        now = float(self._clock())
        with self._nonce_lock:
            cutoff = now - self._replay_ttl_s
            self._seen_nonces = {
                value: seen_at for value, seen_at in self._seen_nonces.items() if seen_at >= cutoff
            }
            if nonce in self._seen_nonces:
                return False
            self._seen_nonces[nonce] = now
            return True

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...],
        operation: str,
        params: Mapping[str, str] | None = None,
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
                detail=f"Vobiz {operation} did not return a definitive result.",
            ) from exc
        if response.status_code not in expected:
            if 400 <= response.status_code < 500:
                raise VoiceyError(
                    "VY-TEL-004",
                    detail=f"Vobiz {operation} http_{response.status_code}.",
                )
            raise VoiceyError(
                "VY-TEL-011",
                detail=f"Vobiz {operation} did not return a definitive result.",
            )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            document = response.json()
        except ValueError as exc:
            raise VoiceyError(
                "VY-TEL-011",
                detail=f"Vobiz {operation} returned invalid JSON.",
            ) from exc
        if not isinstance(document, dict):
            raise VoiceyError(
                "VY-TEL-011",
                detail=f"Vobiz {operation} returned an invalid envelope.",
            )
        return cast("dict[str, object]", document)


def _number_info(item: dict[str, object]) -> NumberInfo:
    capabilities: set[str] = set()
    raw_capabilities = item.get("capabilities")
    if isinstance(raw_capabilities, dict):
        capabilities.update(
            str(name)
            for name, enabled in cast("dict[object, object]", raw_capabilities).items()
            if bool(enabled)
        )
    if bool(item.get("voice_enabled")):
        capabilities.add("voice")
    return NumberInfo(
        number=validate_e164(str(item.get("e164", ""))),
        provider_id=_provider_id(str(item.get("id", "")), field_name="number id"),
        friendly_name=_optional(item.get("friendly_name")),
        country=_optional(item.get("country")),
        locality=_optional(item.get("locality")),
        region=_optional(item.get("region")),
        capabilities=frozenset(capabilities),
    )


def _route_from_number(item: Mapping[str, object]) -> RouteSettings:
    return {
        "application_id": _optional(item.get("application_id")),
        "trunk_group_id": _optional(item.get("trunk_group_id")),
    }


def _status_event(
    event_name: str,
    status: str,
    form: object | None,
) -> tuple[
    Literal["initiated", "ringing", "answered", "completed", "failed"],
    Literal["provider_hangup", "carrier_error"] | None,
]:
    event = event_name.casefold()
    normalized = status.casefold()
    if event in {"ring", "ringing"} or normalized == "ringing":
        return "ringing", None
    if event in {"startapp", "answer", "answered", "startstream"} or normalized in {
        "answered",
        "in-progress",
    }:
        return "answered", None
    if event in {"hangup", "completed"} or normalized in {
        "completed",
        "hangup",
        "failed",
        "busy",
        "no-answer",
        "canceled",
    }:
        cause = (
            _form_value(form, "HangupCause", required=False)
            or _form_value(form, "HangupCauseCode", required=False)
        ).casefold()
        normal = normalized == "completed" or cause in _NORMAL_HANGUPS
        return ("completed", "provider_hangup") if normal else ("failed", "carrier_error")
    if event in {"", "init", "initiated"} and normalized in {"", "queued", "initiated"}:
        return "initiated", None
    raise VoiceyError(
        "VY-TEL-008",
        detail=f"unknown Vobiz event/status {event_name!r}/{status!r}.",
    )


def _direction(value: str) -> Literal["inbound", "outbound"] | None:
    if not value:
        return None
    normalized = value.casefold()
    if normalized in {"incoming", "inbound"}:
        return "inbound"
    if normalized in {"outgoing", "outbound"}:
        return "outbound"
    raise VoiceyError("VY-TEL-008", detail="Vobiz callback direction is invalid.")


def _validated_public_base(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value.rstrip("/"))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise VoiceyError(
            "VY-TEL-002",
            detail="Vobiz expected public base must be an HTTPS origin.",
        )
    return value.rstrip("/")


def _pipecat_target(target: RuntimeTarget) -> PipecatTarget:
    if isinstance(target, LiveKitTarget):
        raise VoiceyError(
            "VY-TEL-002",
            detail="LiveKit targets must use the ledgered Vobiz SIP provisioner.",
        )
    return target


def _header(headers: Mapping[str, str], name: str) -> str:
    folded = name.casefold()
    return next(
        (str(value) for key, value in headers.items() if str(key).casefold() == folded),
        "",
    )


def _form_value(form: object | None, name: str, *, required: bool = True) -> str:
    value: object | None = None
    getter = getattr(form, "get", None)
    if callable(getter):
        value = getter(name)
    if value is None or value == "":
        if required:
            raise VoiceyError("VY-TEL-008", detail=f"Vobiz callback lacks {name}.")
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    raise VoiceyError("VY-TEL-008", detail=f"Vobiz callback {name} is malformed.")


def _provider_id(value: str, *, field_name: str) -> str:
    if not _PROVIDER_ID.fullmatch(value):
        raise VoiceyError("VY-TEL-008", detail=f"invalid Vobiz {field_name}.")
    return value


def _validate_dtmf(value: str) -> str:
    if not _DTMF.fullmatch(value):
        raise VoiceyError("VY-TEL-002", detail="DTMF digits contain unsupported characters.")
    return value


def _optional(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    raise VoiceyError("VY-TEL-011", detail="Vobiz response field has an invalid scalar type.")


def _integer(value: object, *, default: int) -> int:
    if value is None:
        return default
    try:
        return int(cast("Any", value))
    except (TypeError, ValueError) as exc:
        raise VoiceyError("VY-TEL-011", detail="Vobiz pagination total is invalid.") from exc
