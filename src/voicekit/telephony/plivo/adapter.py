"""Beta Plivo Voice API, Plivo XML, media-stream, and callback adapter."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote, urlsplit

import httpx
from plivo.utils import validate_v3_signature  # pyright: ignore[reportUnknownVariableType]

from voicekit.errors import VoicekitError
from voicekit.storage.artifacts import ArtifactStore
from voicekit.telephony.ledger import OutboundIntent, RouteSettings, TelephonyLedger
from voicekit.telephony.models import (
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

_AUTH_ID = re.compile(r"^(?:MA|SA)[A-Za-z0-9]{18}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_AREA = re.compile(r"^[0-9]{1,8}$")
_DTMF = re.compile(r"^[0-9*#wW]{1,64}$")
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9:_-]{1,256}$")
_NONCE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
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
_ValidateV3 = Callable[[str, str, str, str, str, dict[str, str]], bool]
_validate_v3 = cast("_ValidateV3", validate_v3_signature)


class PlivoAdapter:
    """Plivo numbers, calls, Plivo XML, signed callbacks, and media control."""

    provider = "plivo"
    capabilities = Capabilities(
        inbound=True,
        outbound=True,
        amd=True,
        dtmf_receive=True,
        dtmf_send=True,
        transfer_modes=frozenset({"cold"}),
        recording=True,
        regions=("Global", "India"),
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
        base_url: str = "https://api.plivo.com",
        expected_public_base: str | None = None,
        replay_ttl_s: int = 600,
        clock: Any = time.monotonic,
    ) -> None:
        self.auth_id = auth_id or os.environ.get("PLIVO_AUTH_ID", "")
        self._auth_token = auth_token or os.environ.get("PLIVO_AUTH_TOKEN", "")
        if not _AUTH_ID.fullmatch(self.auth_id):
            raise VoicekitError("VK-TEL-002", detail="PLIVO_AUTH_ID is missing or invalid.")
        if not self._auth_token:
            raise VoicekitError("VK-TEL-002", detail="PLIVO_AUTH_TOKEN is required.")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise VoicekitError("VK-TEL-002", detail="Plivo base URL must be normalized HTTPS.")
        if not 60 <= replay_ttl_s <= 3600:
            raise VoicekitError(
                "VK-TEL-002",
                detail="Plivo callback replay TTL must be between 60 and 3600 seconds.",
            )
        self._expected_public_base = _validated_public_base(expected_public_base)
        self._ledger = ledger or TelephonyLedger(
            ledger_path or Path(".voicekit") / "telephony.sqlite3"
        )
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=httpx.BasicAuth(self.auth_id, self._auth_token),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
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
        """Expose durable routing and outbound-intent evidence."""
        return self._ledger

    @property
    def _account_path(self) -> str:
        return f"/v1/Account/{quote(self.auth_id, safe='')}"

    @property
    def _call_path(self) -> str:
        return f"{self._account_path}/Call/"

    def account_state(self) -> CarrierAccountState:
        data = self._request(
            "GET",
            f"{self._account_path}/",
            expected=(200,),
            operation="inspect account",
        )
        return CarrierAccountState(
            provider=self.provider,
            status=str(data.get("account_status", data.get("status", "active"))),
            account_type=_optional(data.get("account_type")),
            balance=_optional(data.get("cash_credits", data.get("balance"))),
            currency=_optional(data.get("currency")) or "USD",
        )

    def list_numbers(self) -> list[NumberInfo]:
        return [_number_info(item) for item in self._list_pages("Number")]

    def buy_number(self, country: str, area: str | None = None) -> NumberInfo:
        normalized_country = country.upper()
        if not _COUNTRY.fullmatch(normalized_country):
            raise VoicekitError("VK-TEL-002", detail="country must be an ISO-3166 alpha-2 code.")
        if area is not None and not _AREA.fullmatch(area):
            raise VoicekitError(
                "VK-TEL-002",
                detail="Plivo number search prefix must contain 1-8 digits.",
            )
        response = self._request(
            "GET",
            f"{self._account_path}/PhoneNumber/",
            params={
                "country_iso": normalized_country,
                "services": "voice",
                "limit": "20",
                **({} if area is None else {"pattern": area}),
            },
            expected=(200,),
            operation="search numbers",
        )
        candidates = _objects(response, operation="number search")
        if not candidates:
            raise VoicekitError(
                "VK-TEL-003",
                detail=f"no Plivo number is available for {normalized_country}/{area or '*'}.",
            )
        candidate = candidates[0]
        number = validate_e164(_e164(candidate.get("number", candidate.get("e164", ""))))
        purchased = self._request(
            "POST",
            f"{self._account_path}/PhoneNumber/{quote(number.removeprefix('+'), safe='')}/",
            expected=(200, 201, 202),
            operation="buy number",
        )
        provider_id = _optional(purchased.get("number")) or number.removeprefix("+")
        return NumberInfo(
            number=number,
            provider_id=_provider_id(provider_id, field_name="number id"),
            country=normalized_country,
            locality=_optional(candidate.get("city")),
            region=_optional(candidate.get("region")),
            capabilities=frozenset({"voice"}),
        )

    def release_number(self, number: str) -> None:
        owned = self._owned_number(number)
        normalized = validate_e164(_e164(owned.get("number", "")))
        self._request(
            "DELETE",
            f"{self._account_path}/Number/{quote(normalized.removeprefix('+'), safe='')}/",
            expected=(202, 204),
            operation="release number",
        )

    def inbound_route(self, number: str) -> dict[str, str | None]:
        return {"app_id": _optional(self._owned_number(number).get("app_id"))}

    def point_inbound(self, number: str, target: RuntimeTarget) -> RollbackToken:
        pipecat = _pipecat_target(target)
        owned = self._owned_number(number)
        normalized = validate_e164(_e164(owned.get("number", "")))
        app_id, created = self._ensure_application(pipecat)
        snapshot: RouteSettings = {"app_id": _optional(owned.get("app_id"))}
        applied: RouteSettings = {
            "app_id": app_id,
            "managed_application_id": app_id if created else None,
        }
        route = self._ledger.prepare_route(
            provider=self.provider,
            number=normalized,
            number_sid=normalized.removeprefix("+"),
            snapshot=snapshot,
            applied=applied,
        )
        try:
            self._update_number(normalized, app_id)
            confirmed = self.inbound_route(normalized)
        except VoicekitError as exc:
            state: Literal["failed", "ambiguous"] = (
                "failed" if exc.code == "VK-TEL-004" else "ambiguous"
            )
            self._ledger.transition_route(route.token, expected=("prepared",), state=state)
            if state == "ambiguous":
                raise VoicekitError(
                    "VK-TEL-006",
                    detail=f"Plivo route outcome is ambiguous for token {route.token!r}.",
                ) from exc
            raise
        if confirmed != {"app_id": app_id}:
            self._ledger.transition_route(route.token, expected=("prepared",), state="ambiguous")
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"Plivo did not confirm route token {route.token!r}.",
            )
        self._ledger.transition_route(route.token, expected=("prepared",), state="applied")
        return RollbackToken(provider=self.provider, token=route.token)

    def restore(self, token: RollbackToken) -> None:
        if token.provider != self.provider:
            raise VoicekitError("VK-TEL-006", detail="routing token belongs to another carrier.")
        route = self._ledger.get_route(token.token)
        if route.provider != self.provider:
            raise VoicekitError("VK-TEL-006", detail="routing token belongs to another carrier.")
        if route.state == "restored":
            return
        if route.state not in {"applied", "ambiguous", "prepared"}:
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"routing token {token.token!r} cannot restore from {route.state!r}.",
            )
        current = self.inbound_route(route.number)
        comparable = {"app_id": route.applied.get("app_id")}
        if current != route.snapshot and current != comparable:
            self._ledger.transition_route(token.token, expected=(route.state,), state="conflict")
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"Plivo route changed after token {token.token!r} was applied.",
            )
        try:
            if current != route.snapshot:
                self._update_number(route.number, route.snapshot.get("app_id"))
                if self.inbound_route(route.number) != route.snapshot:
                    raise VoicekitError(
                        "VK-TEL-006",
                        detail=f"Plivo route restore did not compare equal for {token.token!r}.",
                    )
            managed = route.applied.get("managed_application_id")
            if managed:
                self._request(
                    "DELETE",
                    f"{self._account_path}/Application/{quote(managed, safe='')}/",
                    expected=(202, 204),
                    operation="delete managed application",
                )
        except VoicekitError as exc:
            self._ledger.transition_route(token.token, expected=(route.state,), state="conflict")
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"Plivo route restore conflicted for token {token.token!r}.",
            ) from exc
        self._ledger.transition_route(token.token, expected=(route.state,), state="restored")

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
            raise VoicekitError("VK-TEL-002", detail="call timeout must be between 5 and 600s.")
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
        body: dict[str, object] = {
            "from": from_number,
            "to": to_number,
            "answer_url": f"{pipecat.answer_url}/{quote(durable_id, safe='')}",
            "answer_method": "POST",
            "ring_url": pipecat.event_url(durable_id),
            "ring_method": "POST",
            "hangup_url": pipecat.event_url(durable_id),
            "hangup_method": "POST",
            "ring_timeout": timeout_s,
        }
        if digits is not None:
            body["send_digits"] = digits
        if amd:
            body.update(
                {
                    "machine_detection": "true",
                    "machine_detection_url": f"{pipecat.amd_url}/{quote(durable_id, safe='')}",
                    "machine_detection_method": "POST",
                }
            )
        try:
            data = self._request(
                "POST",
                self._call_path,
                json_body=body,
                expected=(200, 201, 202),
                operation="start outbound call",
            )
        except VoicekitError as exc:
            if exc.code == "VK-TEL-004":
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
            raise VoicekitError(
                "VK-TEL-007",
                detail=f"reconcile Plivo outbound intent {durable_id!r}; it was not retried.",
            ) from exc
        raw_call_id = data.get("request_uuid", data.get("call_uuid"))
        if isinstance(raw_call_id, list):
            call_ids = cast("list[object]", raw_call_id)
            raw_call_id = call_ids[0] if len(call_ids) == 1 else ""
        try:
            call_id = _provider_id(str(raw_call_id or ""), field_name="call UUID")
        except VoicekitError as exc:
            self._ledger.transition_intent(
                durable_id,
                expected=("prepared",),
                state="ambiguous",
                last_status="malformed_create_response",
            )
            raise VoicekitError(
                "VK-TEL-007",
                detail=f"Plivo accepted outbound intent {durable_id!r} without one call UUID.",
            ) from exc
        try:
            self._ledger.transition_intent(
                durable_id,
                expected=("prepared",),
                state="submitted",
                provider_call_id=call_id,
                last_status="queued",
            )
        except VoicekitError as exc:
            raise VoicekitError(
                "VK-TEL-007",
                detail=f"Plivo accepted outbound intent {durable_id!r}; reconcile it.",
            ) from exc
        return call_id

    def reconcile_outbound(self, intent_id: str) -> OutboundIntent:
        intent = self._ledger.get_intent(intent_id)
        if intent.provider_call_id is not None:
            return intent
        raise VoicekitError(
            "VK-TEL-007",
            detail=f"outbound intent {intent_id!r} has no signed Plivo callback; do not retry.",
        )

    def start_recording(self, call_uuid: str, target: RuntimeTarget) -> str:
        call_id = _provider_id(call_uuid, field_name="call UUID")
        pipecat = _pipecat_target(target)
        data = self._request(
            "POST",
            f"{self._call_path}{quote(call_id, safe='')}/Record/",
            json_body={
                "time_limit": 86400,
                "file_format": "mp3",
                "record_channel_type": "stereo",
                "callback_url": pipecat.recording_url,
                "callback_method": "POST",
            },
            expected=(200, 201, 202),
            operation="start recording",
        )
        return _provider_id(str(data.get("recording_id", "")), field_name="recording id")

    def send_dtmf(self, call_uuid: str, digits: str) -> None:
        call_id = _provider_id(call_uuid, field_name="call UUID")
        self._request(
            "POST",
            f"{self._call_path}{quote(call_id, safe='')}/DTMF/",
            json_body={"digits": _validate_dtmf(digits), "leg": "aleg"},
            expected=(200, 202),
            operation="send DTMF",
        )

    def cold_transfer(self, call_uuid: str, to_number: str) -> None:
        if self._expected_public_base is None:
            raise VoicekitError(
                "VK-TEL-002",
                detail="Plivo transfer requires expected_public_base.",
            )
        call_id = _provider_id(call_uuid, field_name="call UUID")
        destination = validate_e164(to_number)
        self._request(
            "POST",
            f"{self._call_path}{quote(call_id, safe='')}/",
            json_body={
                "legs": "aleg",
                "aleg_url": f"{self._expected_public_base}/plivo/transfer/{destination}",
                "aleg_method": "POST",
            },
            expected=(200, 202),
            operation="cold transfer",
        )

    def hangup(self, call_uuid: str) -> None:
        call_id = _provider_id(call_uuid, field_name="call UUID")
        self._request(
            "DELETE",
            f"{self._call_path}{quote(call_id, safe='')}/",
            expected=(202, 204, 404),
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
            raise VoicekitError("VK-TEL-009", detail="Plivo recording URL is not safe HTTPS.")
        if max_bytes <= 0:
            raise VoicekitError("VK-TEL-009", detail="recording size limit must be positive.")
        client = self._recording_client or httpx.AsyncClient(
            auth=httpx.BasicAuth(self.auth_id, self._auth_token),
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
                    raise VoicekitError("VK-TEL-009", detail="recording exceeds size limit.")
                media_type = response.headers.get("content-type", "").split(";", maxsplit=1)[0]
                if media_type not in {
                    "audio/mpeg",
                    "audio/mp3",
                    "audio/wav",
                    "audio/x-wav",
                    "application/octet-stream",
                }:
                    raise VoicekitError(
                        "VK-TEL-009",
                        detail="recording response has an unexpected content type.",
                    )
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise VoicekitError("VK-TEL-009", detail="recording exceeds size limit.")
            await artifact_store.put(storage_key, bytes(content))
        except VoicekitError:
            raise
        except (ValueError, httpx.HTTPError) as exc:
            raise VoicekitError("VK-TEL-009", detail="recording download failed.") from exc
        finally:
            if owns_client:
                await client.aclose()
        return storage_key

    def verify_request(self, request: TelephonyRequest) -> bool:
        """Validate installed-SDK V3 signatures and reject replayed nonces."""
        if request.is_websocket or request.scheme.casefold() != "https":
            return False
        nonce = _header(request.headers, "x-plivo-signature-v3-nonce")
        signature = _header(request.headers, "x-plivo-signature-v3")
        if not nonce or not signature or not _NONCE.fullmatch(nonce):
            return False
        callback_url = self._callback_url(request)
        if callback_url is None:
            return False
        params = _form_dict(request.form)
        try:
            valid = bool(
                _validate_v3(
                    "POST",
                    callback_url,
                    nonce,
                    self._auth_token,
                    signature,
                    params,
                )
            )
        except (TypeError, ValueError):
            return False
        return valid and self._claim_nonce(nonce)

    def answer_response(self, target: RuntimeTarget) -> str:
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
                "statusCallbackUrl": pipecat.event_url(),
                "statusCallbackMethod": "POST",
            },
        )
        stream.text = pipecat.stream_url
        return ET.tostring(response, encoding="utf-8", xml_declaration=True).decode()

    def transfer_response(self, to_number: str, *, caller_id: str | None = None) -> str:
        destination = validate_e164(to_number)
        response = ET.Element("Response")
        attributes = {} if caller_id is None else {"callerId": validate_e164(caller_id)}
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
            request.form, "RecordingUrl", required=False
        )
        recording_id = _form_value(request.form, "recording_id", required=False) or _form_value(
            request.form, "RecordingID", required=False
        )
        if recording_url or recording_id:
            if not recording_url or not recording_id:
                raise VoicekitError("VK-TEL-008", detail="Plivo recording callback is incomplete.")
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

    def _owned_number(self, number: str) -> dict[str, object]:
        normalized = validate_e164(number)
        response = self._request(
            "GET",
            f"{self._account_path}/Number/{quote(normalized.removeprefix('+'), safe='')}/",
            expected=(200,),
            operation="retrieve owned number",
        )
        returned = validate_e164(_e164(response.get("number", "")))
        if returned != normalized:
            raise VoicekitError("VK-TEL-003", detail="Plivo number ownership could not be proved.")
        return response

    def _update_number(self, number: str, app_id: str | None) -> None:
        normalized = validate_e164(number).removeprefix("+")
        self._request(
            "POST",
            f"{self._account_path}/Number/{quote(normalized, safe='')}/",
            json_body={"app_id": app_id or ""},
            expected=(200, 202),
            operation="update number application",
        )

    def _ensure_application(self, target: PipecatTarget) -> tuple[str, bool]:
        fingerprint = hashlib.sha256(
            f"{target.answer_url}|{target.event_url()}".encode()
        ).hexdigest()[:20]
        name = f"voicekit-{fingerprint}"
        apps = self._list_pages("Application", params={"app_name": name})
        matches = [item for item in apps if str(item.get("app_name", "")) == name]
        if len(matches) > 1:
            raise VoicekitError("VK-TEL-006", detail="duplicate managed Plivo applications.")
        if matches:
            app = matches[0]
            if (
                str(app.get("answer_url", "")) != target.answer_url
                or str(app.get("answer_method", "")).upper() != "POST"
                or str(app.get("hangup_url", "")) != target.event_url()
                or str(app.get("hangup_method", "")).upper() != "POST"
            ):
                raise VoicekitError(
                    "VK-TEL-006",
                    detail="managed Plivo application differs from desired config.",
                )
            return _provider_id(str(app.get("app_id", "")), field_name="app id"), False
        created = self._request(
            "POST",
            f"{self._account_path}/Application/",
            json_body={
                "app_name": name,
                "answer_url": target.answer_url,
                "answer_method": "POST",
                "hangup_url": target.event_url(),
                "hangup_method": "POST",
                "default_number_app": False,
            },
            expected=(200, 201),
            operation="create application",
        )
        return _provider_id(str(created.get("app_id", "")), field_name="app id"), True

    def _list_pages(
        self,
        resource: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> list[dict[str, object]]:
        offset = 0
        found: list[dict[str, object]] = []
        while True:
            response = self._request(
                "GET",
                f"{self._account_path}/{resource}/",
                params={**(params or {}), "limit": "20", "offset": str(offset)},
                expected=(200,),
                operation=f"list {resource.casefold()}",
            )
            page = _objects(response, operation=f"{resource.casefold()} list")
            found.extend(page)
            meta = response.get("meta")
            next_page = (
                cast("dict[str, object]", meta).get("next") if isinstance(meta, dict) else None
            )
            if not next_page or not page:
                return found
            offset += len(page)

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
            response = self._client.request(method, path, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise VoicekitError(
                "VK-TEL-011",
                detail=f"Plivo {operation} did not return a definitive result.",
            ) from exc
        if response.status_code not in expected:
            if 400 <= response.status_code < 500:
                raise VoicekitError(
                    "VK-TEL-004",
                    detail=f"Plivo {operation} http_{response.status_code}.",
                )
            raise VoicekitError(
                "VK-TEL-011",
                detail=f"Plivo {operation} did not return a definitive result.",
            )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            document = response.json()
        except ValueError as exc:
            raise VoicekitError(
                "VK-TEL-011",
                detail=f"Plivo {operation} returned invalid JSON.",
            ) from exc
        if not isinstance(document, dict):
            raise VoicekitError(
                "VK-TEL-011",
                detail=f"Plivo {operation} returned an invalid envelope.",
            )
        return cast("dict[str, object]", document)


def _objects(document: Mapping[str, object], *, operation: str) -> list[dict[str, object]]:
    raw = document.get("objects", [])
    if not isinstance(raw, list):
        raise VoicekitError("VK-TEL-011", detail=f"Plivo {operation} is malformed.")
    values = cast("list[object]", raw)
    if any(not isinstance(item, dict) for item in values):
        raise VoicekitError("VK-TEL-011", detail=f"Plivo {operation} is malformed.")
    return cast("list[dict[str, object]]", values)


def _number_info(item: Mapping[str, object]) -> NumberInfo:
    number = validate_e164(_e164(item.get("number", "")))
    capabilities: set[str] = set()
    services = item.get("services")
    if isinstance(services, list):
        capabilities.update(str(value) for value in cast("list[object]", services))
    if item.get("voice_enabled") is True:
        capabilities.add("voice")
    return NumberInfo(
        number=number,
        provider_id=_provider_id(number.removeprefix("+"), field_name="number id"),
        friendly_name=_optional(item.get("alias")),
        country=_optional(item.get("country_iso", item.get("country"))),
        locality=_optional(item.get("city")),
        region=_optional(item.get("region")),
        capabilities=frozenset(capabilities),
    )


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
    raise VoicekitError(
        "VK-TEL-008",
        detail=f"unknown Plivo event/status {event_name!r}/{status!r}.",
    )


def _direction(value: str) -> Literal["inbound", "outbound"] | None:
    if not value:
        return None
    normalized = value.casefold()
    if normalized in {"incoming", "inbound"}:
        return "inbound"
    if normalized in {"outgoing", "outbound"}:
        return "outbound"
    raise VoicekitError("VK-TEL-008", detail="Plivo callback direction is invalid.")


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
        raise VoicekitError(
            "VK-TEL-002",
            detail="Plivo expected public base must be an HTTPS origin.",
        )
    return value.rstrip("/")


def _pipecat_target(target: RuntimeTarget) -> PipecatTarget:
    if isinstance(target, LiveKitTarget):
        raise VoicekitError(
            "VK-TEL-002",
            detail="LiveKit targets must use the ledgered Plivo SIP provisioner.",
        )
    return target


def _header(headers: Mapping[str, str], name: str) -> str:
    folded = name.casefold()
    return next(
        (str(value) for key, value in headers.items() if str(key).casefold() == folded),
        "",
    )


def _form_dict(form: object | None) -> dict[str, str]:
    if not isinstance(form, Mapping):
        return {}
    values = cast("Mapping[object, object]", form)
    return {str(key): str(value) for key, value in values.items()}


def _form_value(form: object | None, name: str, *, required: bool = True) -> str:
    value: object | None = None
    getter = getattr(form, "get", None)
    if callable(getter):
        value = getter(name)
    if value is None or value == "":
        if required:
            raise VoicekitError("VK-TEL-008", detail=f"Plivo callback lacks {name}.")
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    raise VoicekitError("VK-TEL-008", detail=f"Plivo callback {name} is malformed.")


def _provider_id(value: str, *, field_name: str) -> str:
    if not _PROVIDER_ID.fullmatch(value):
        raise VoicekitError("VK-TEL-008", detail=f"invalid Plivo {field_name}.")
    return value


def _validate_dtmf(value: str) -> str:
    if not _DTMF.fullmatch(value):
        raise VoicekitError("VK-TEL-002", detail="DTMF digits contain unsupported characters.")
    return value


def _optional(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    raise VoicekitError("VK-TEL-011", detail="Plivo response field has an invalid scalar type.")


def _e164(value: object) -> str:
    text = str(value)
    return text if text.startswith("+") else f"+{text}"
