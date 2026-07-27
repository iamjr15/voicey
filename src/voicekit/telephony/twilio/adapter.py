"""Certified Twilio adapter on the installed 9.10.9 helper-library surface."""

from __future__ import annotations

import os
import re
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, NoReturn, cast
from urllib.parse import urlsplit

import httpx
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

from voicekit.errors import VoicekitError
from voicekit.storage.artifacts import ArtifactStore
from voicekit.telephony.ledger import OutboundIntent, TelephonyLedger
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

_CALL_SID = re.compile(r"^CA[0-9a-fA-F]{32}$")
_ACCOUNT_SID = re.compile(r"^AC[0-9a-fA-F]{32}$")
_NUMBER_SID = re.compile(r"^PN[0-9a-fA-F]{32}$")
_RECORDING_SID = re.compile(r"^RE[0-9a-fA-F]{32}$")
_DTMF = re.compile(r"^[0-9*#wW]{1,32}$")
_ROUTE_FIELDS = (
    "voice_url",
    "voice_method",
    "voice_fallback_url",
    "voice_fallback_method",
    "status_callback",
    "status_callback_method",
    "voice_application_sid",
    "trunk_sid",
)
_TERMINAL_STATUSES = frozenset({"completed", "busy", "no-answer", "failed", "canceled"})


class TwilioAdapter:
    """Twilio number, webhook, call, control, and recording operations."""

    provider = "twilio"
    capabilities = Capabilities(
        inbound=True,
        outbound=True,
        amd=True,
        dtmf_receive=True,
        dtmf_send=True,
        transfer_modes=frozenset({"cold", "warm"}),
        recording=True,
        regions=("US1", "IE1", "AU1"),
        native_outbound_idempotency=False,
        livekit_sip=True,
    )

    def __init__(
        self,
        *,
        account_sid: str | None = None,
        auth_token: str | None = None,
        ledger: TelephonyLedger | None = None,
        ledger_path: Path | None = None,
        client: Any | None = None,
        expected_public_base: str | None = None,
        trusted_proxies: frozenset[str] = frozenset({"127.0.0.1", "::1"}),
        recording_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.account_sid = account_sid or os.environ.get("TWILIO_ACCOUNT_SID", "")
        self._auth_token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN", "")
        if not _ACCOUNT_SID.fullmatch(self.account_sid) or not self._auth_token:
            raise VoicekitError(
                "VK-TEL-002",
                detail="TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are required.",
            )
        if expected_public_base is not None:
            parsed = urlsplit(expected_public_base)
            if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
                raise VoicekitError(
                    "VK-TEL-002",
                    detail="expected_public_base must be an HTTPS origin/base path.",
                )
        self.expected_public_base = (
            None if expected_public_base is None else expected_public_base.rstrip("/")
        )
        self._trusted_proxies = trusted_proxies
        self._client: Any = (
            client if client is not None else Client(self.account_sid, self._auth_token)
        )
        self._ledger = ledger or TelephonyLedger(
            ledger_path or Path(".voicekit") / "telephony.sqlite3"
        )
        self._recording_client = recording_client
        self._validator = RequestValidator(self._auth_token)

    @property
    def ledger(self) -> TelephonyLedger:
        """Expose durable evidence to CLI reconciliation commands."""
        return self._ledger

    def list_numbers(self) -> list[NumberInfo]:
        try:
            resources = self._client.incoming_phone_numbers.list()
        except Exception as exc:
            _raise_carrier(exc, operation="list numbers")
        return [_number_info(resource) for resource in cast("list[Any]", resources)]

    def account_state(self) -> CarrierAccountState:
        """Fetch safe trial/funding facts for doctor without returning credentials."""
        try:
            account = self._client.api.accounts(self.account_sid).fetch()
            balance = self._client.api.v2010.accounts(self.account_sid).balance.fetch()
        except Exception as exc:
            _raise_carrier(exc, operation="inspect account")
        return CarrierAccountState(
            provider=self.provider,
            status=str(account.status),
            account_type=None if account.type is None else str(account.type),
            balance=None if balance.balance is None else str(balance.balance),
            currency=None if balance.currency is None else str(balance.currency),
        )

    def inbound_route(self, number: str) -> dict[str, str | None]:
        """Fetch the current complete inbound route for doctor diffing."""
        return _route_settings(self._owned_number(number))

    def buy_number(self, country: str, area: str | None = None) -> NumberInfo:
        normalized_country = country.upper()
        if not re.fullmatch(r"[A-Z]{2}", normalized_country):
            raise VoicekitError("VK-TEL-002", detail="country must be an ISO-3166 alpha-2 code.")
        if area is not None and not area.isdigit():
            raise VoicekitError("VK-TEL-002", detail="Twilio area code must contain only digits.")
        try:
            local = self._client.available_phone_numbers(normalized_country).local
            search: dict[str, object] = {"voice_enabled": True, "limit": 20}
            if area is not None:
                search["area_code"] = int(area)
            candidates = local.list(**search)
            if not candidates:
                raise VoicekitError(
                    "VK-TEL-003",
                    detail=f"no voice number is available for {normalized_country}/{area or '*'}.",
                )
            chosen = candidates[0]
            created = self._client.incoming_phone_numbers.create(
                phone_number=str(chosen.phone_number)
            )
        except VoicekitError:
            raise
        except Exception as exc:
            _raise_carrier(exc, operation="buy number")
        return _number_info(created)

    def release_number(self, number: str) -> None:
        resource = self._owned_number(number)
        try:
            deleted = bool(self._client.incoming_phone_numbers(resource.sid).delete())
        except Exception as exc:
            _raise_carrier(exc, operation="release number")
        if not deleted:
            raise VoicekitError("VK-TEL-004", detail="Twilio did not confirm number release.")

    def point_inbound(
        self,
        number: str,
        target: RuntimeTarget,
    ) -> RollbackToken:
        pipecat = _pipecat_target(target)
        resource = self._owned_number(number)
        snapshot = _route_settings(resource)
        applied = {
            "voice_url": pipecat.answer_url,
            "voice_method": "POST",
            "voice_fallback_url": None,
            "voice_fallback_method": "POST",
            "status_callback": pipecat.event_url(),
            "status_callback_method": "POST",
            "voice_application_sid": None,
            "trunk_sid": None,
        }
        record = self._ledger.prepare_route(
            provider=self.provider,
            number=str(resource.phone_number),
            number_sid=str(resource.sid),
            snapshot=snapshot,
            applied=applied,
        )
        try:
            updated = self._client.incoming_phone_numbers(resource.sid).update(
                **_route_update_arguments(applied)
            )
        except Exception as exc:
            if _definitive_rejection(exc):
                self._ledger.transition_route(
                    record.token,
                    expected=("prepared",),
                    state="failed",
                )
                _raise_carrier(exc, operation="point inbound number")
            self._ledger.transition_route(
                record.token,
                expected=("prepared",),
                state="ambiguous",
            )
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"routing outcome is ambiguous for token {record.token!r}.",
            ) from exc
        if _route_settings(updated) != applied:
            self._ledger.transition_route(
                record.token,
                expected=("prepared",),
                state="ambiguous",
            )
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"Twilio did not confirm the complete route for token {record.token!r}.",
            )
        self._ledger.transition_route(
            record.token,
            expected=("prepared",),
            state="applied",
        )
        return RollbackToken(provider=self.provider, token=record.token)

    def restore(self, token: RollbackToken) -> None:
        if token.provider != self.provider:
            raise VoicekitError("VK-TEL-006", detail="rollback token belongs to another provider.")
        record = self._ledger.get_route(token.token)
        if record.state == "restored":
            return
        try:
            context = self._client.incoming_phone_numbers(record.number_sid)
            current = _route_settings(context.fetch())
        except Exception as exc:
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"cannot reconcile routing token {record.token!r}.",
            ) from exc
        if current == record.snapshot:
            self._ledger.transition_route(
                record.token,
                expected=(record.state,),
                state="restored",
            )
            return
        if current != record.applied:
            self._ledger.transition_route(
                record.token,
                expected=(record.state,),
                state="conflict",
            )
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"current Twilio routing does not match token {record.token!r}.",
            )
        try:
            restored = context.update(**_route_update_arguments(record.snapshot))
        except Exception as exc:
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"restore outcome is ambiguous for token {record.token!r}.",
            ) from exc
        if _route_settings(restored) != record.snapshot:
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"Twilio did not confirm restore for token {record.token!r}.",
            )
        self._ledger.transition_route(
            record.token,
            expected=(record.state,),
            state="restored",
        )

    def recover_routes(self) -> int:
        """Restore every open temporary route after an interrupted dev process."""
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
        if send_digits is not None:
            _validate_dtmf(send_digits)
        durable_id = validate_identifier(
            intent_id or f"intent_{uuid.uuid4().hex}",
            field_name="outbound intent id",
        )
        target_record: dict[str, object] = {
            "https_base": pipecat.https_base,
            "ws_path": pipecat.ws_path,
            "record": record,
            "amd": amd,
        }
        self._ledger.prepare_intent(
            intent_id=durable_id,
            provider=self.provider,
            from_number=from_number,
            to_number=to_number,
            target=target_record,
        )
        direct_twiml = self._stream_twiml(pipecat, {"intent_id": durable_id})
        create_arguments: dict[str, object] = {
            "to": to_number,
            "from_": from_number,
            "twiml": _amd_hold_twiml() if amd else direct_twiml,
            "status_callback": pipecat.event_url(durable_id),
            "status_callback_event": ["initiated", "ringing", "answered", "completed"],
            "status_callback_method": "POST",
            "timeout": timeout_s,
            "record": record,
        }
        if send_digits is not None:
            create_arguments["send_digits"] = send_digits
        if record:
            create_arguments.update(
                {
                    "recording_channels": "dual",
                    "recording_status_callback": pipecat.recording_url,
                    "recording_status_callback_method": "POST",
                    "recording_status_callback_event": ["completed", "absent"],
                }
            )
        if amd:
            create_arguments.update(
                {
                    "machine_detection": "Enable",
                    "async_amd": "true",
                    "async_amd_status_callback": pipecat.amd_url,
                    "async_amd_status_callback_method": "POST",
                }
            )
        try:
            call = self._client.calls.create(**create_arguments)
        except Exception as exc:
            if _definitive_rejection(exc):
                self._ledger.transition_intent(
                    durable_id,
                    expected=("prepared",),
                    state="rejected",
                    last_status=_safe_carrier_status(exc),
                )
                _raise_carrier(exc, operation="start outbound call")
            self._ledger.transition_intent(
                durable_id,
                expected=("prepared",),
                state="ambiguous",
                last_status="create_outcome_unknown",
            )
            raise VoicekitError(
                "VK-TEL-007",
                detail=f"reconcile outbound intent {durable_id!r}; it was not retried.",
            ) from exc
        try:
            call_sid = _validate_call_sid(str(call.sid))
        except VoicekitError as exc:
            self._ledger.transition_intent(
                durable_id,
                expected=("prepared",),
                state="ambiguous",
                last_status="invalid_provider_call_id",
            )
            raise VoicekitError(
                "VK-TEL-007",
                detail=f"provider accepted outbound intent {durable_id!r}; reconcile it.",
            ) from exc
        try:
            self._ledger.transition_intent(
                durable_id,
                expected=("prepared",),
                state="submitted",
                provider_call_id=call_sid,
                last_status=str(getattr(call, "status", "queued")),
            )
        except VoicekitError as exc:
            raise VoicekitError(
                "VK-TEL-007",
                detail=f"provider accepted outbound intent {durable_id!r}; reconcile it.",
            ) from exc
        return call_sid

    def reconcile_outbound(self, intent_id: str) -> OutboundIntent:
        """Bind only a unique from/to/time candidate; never place a retry."""
        intent = self._ledger.get_intent(intent_id)
        if intent.provider_call_id is not None:
            return intent
        try:
            candidates = cast(
                "list[Any]",
                self._client.calls.list(
                    from_=intent.from_number,
                    to=intent.to_number,
                    start_time_after=intent.created_at - timedelta(minutes=1),
                    start_time_before=intent.created_at + timedelta(minutes=10),
                    limit=3,
                ),
            )
        except Exception as exc:
            _raise_carrier(exc, operation="reconcile outbound call")
        if len(candidates) != 1:
            raise VoicekitError(
                "VK-TEL-007",
                detail=(
                    f"outbound intent {intent_id!r} has {len(candidates)} possible provider calls."
                ),
            )
        candidate = candidates[0]
        return self._ledger.transition_intent(
            intent_id,
            expected=("prepared", "ambiguous"),
            state="reconciled",
            provider_call_id=_validate_call_sid(str(candidate.sid)),
            last_status=str(candidate.status),
        )

    def verify_request(self, request: TelephonyRequest) -> bool:
        if self.expected_public_base is None:
            return False
        signature = _header(request.headers, "x-twilio-signature")
        if not signature:
            return False
        try:
            public_url = self._public_url(request)
        except VoicekitError:
            return False
        if public_url is None:
            return False
        params: object
        if request.is_websocket:
            params = {}
        elif "bodySHA256=" in request.query_string and request.raw_body is not None:
            params = request.raw_body
        else:
            params = request.form or {}
        try:
            return self._validator.validate(public_url, params, signature) is True
        except (AttributeError, TypeError, ValueError):
            return False

    def answer_response(self, target: RuntimeTarget) -> str:
        return self._stream_twiml(_pipecat_target(target), {})

    def parse_event(self, request: TelephonyRequest) -> CallEvent:
        call_sid = _validate_call_sid(_form_value(request.form, "CallSid"))
        intent_id = request.route_params.get("intent_id")
        if intent_id is not None:
            validate_identifier(intent_id, field_name="outbound intent id")
        recording_status = _form_value(request.form, "RecordingStatus", required=False)
        if recording_status:
            recording_sid = _form_value(request.form, "RecordingSid", required=False)
            if recording_status == "completed" and recording_sid:
                event = CallEvent(
                    type="recording_ready",
                    provider_call_id=call_sid,
                    provider_status=recording_status,
                    recording_sid=_validate_recording_sid(recording_sid),
                    recording_url=_form_value(
                        request.form,
                        "RecordingUrl",
                        required=False,
                    ),
                    intent_id=intent_id,
                )
            else:
                event = CallEvent(
                    type="recording_failed",
                    provider_call_id=call_sid,
                    provider_status=recording_status,
                    intent_id=intent_id,
                )
            return event
        answered_by = _form_value(request.form, "AnsweredBy", required=False)
        if answered_by:
            return CallEvent(
                type="amd",
                provider_call_id=call_sid,
                provider_status="amd",
                answered_by=answered_by,
                intent_id=intent_id,
            )
        digits = _form_value(request.form, "Digits", required=False)
        if digits:
            return CallEvent(
                type="dtmf",
                provider_call_id=call_sid,
                provider_status="received",
                digits=_validate_dtmf(digits),
                intent_id=intent_id,
            )
        status = _form_value(request.form, "CallStatus")
        event_type, reason = _status_event(status)
        event = CallEvent(
            type=event_type,
            provider_call_id=call_sid,
            provider_status=status,
            ended_reason=reason,
            intent_id=intent_id,
        )
        if intent_id is not None:
            self._ledger.bind_callback(
                intent_id,
                provider_call_id=call_sid,
                provider_status=status,
                terminal=status in _TERMINAL_STATUSES,
            )
        return event

    def resume_after_amd(
        self,
        call_sid: str,
        *,
        answered_by: str,
        target: RuntimeTarget,
        connect_machine: bool = False,
    ) -> Literal["connected", "hung_up"]:
        """Start media only after async AMD, avoiding competing forked audio."""
        context = self._client.calls(_validate_call_sid(call_sid))
        is_machine = answered_by.startswith("machine") or answered_by == "fax"
        try:
            if is_machine and not connect_machine:
                context.update(status="completed")
                return "hung_up"
            context.update(twiml=self.answer_response(target))
        except Exception as exc:
            _raise_carrier(exc, operation="continue async AMD call")
        return "connected"

    def send_dtmf(self, call_sid: str, digits: str) -> None:
        response = VoiceResponse()
        response.play(digits=_validate_dtmf(digits))
        self._update_call(call_sid, twiml=_checked_twiml(response))

    def cold_transfer(self, call_sid: str, to_number: str) -> None:
        response = VoiceResponse()
        response.dial(validate_e164(to_number), answer_on_bridge=True)
        self._update_call(call_sid, twiml=_checked_twiml(response))

    def hangup(self, call_sid: str) -> None:
        self._update_call(call_sid, status="completed")

    async def download_recording(
        self,
        recording_sid: str,
        *,
        artifact_store: ArtifactStore,
        storage_key: str,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> str:
        """Fetch Basic-authenticated media into engine-owned protected storage."""
        try:
            sid = _validate_recording_sid(recording_sid)
        except VoicekitError as exc:
            raise VoicekitError("VK-TEL-009", detail="invalid Twilio RecordingSid.") from exc
        if max_bytes <= 0:
            raise VoicekitError("VK-TEL-009", detail="recording size limit must be positive.")
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Recordings/{sid}.mp3"
        client = self._recording_client or httpx.AsyncClient(
            auth=httpx.BasicAuth(self.account_sid, self._auth_token),
            timeout=30,
            follow_redirects=False,
        )
        owns_client = self._recording_client is None
        content = bytearray()
        try:
            async with client.stream(
                "GET",
                url,
                auth=httpx.BasicAuth(self.account_sid, self._auth_token),
            ) as response:
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared is not None and int(declared) > max_bytes:
                    raise VoicekitError("VK-TEL-009", detail="recording exceeds size limit.")
                content_type = response.headers.get("content-type", "").split(";", maxsplit=1)[0]
                if content_type not in {
                    "audio/mpeg",
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

    def _stream_twiml(
        self,
        target: PipecatTarget,
        extra_parameters: dict[str, str],
    ) -> str:
        response = VoiceResponse()
        connect = cast("Any", response.connect())
        stream = connect.stream(url=target.stream_url)
        parameters = {**target.custom_parameters, **extra_parameters}
        for name, value in sorted(parameters.items()):
            stream.parameter(name=name, value=value)
        return _checked_twiml(response)

    def _public_url(self, request: TelephonyRequest) -> str | None:
        trusted = request.peer_host in self._trusted_proxies
        scheme = request.scheme
        host = request.host
        if trusted:
            scheme = _first_header_value(_header(request.headers, "x-forwarded-proto") or scheme)
            host = _first_header_value(
                _header(request.headers, "x-forwarded-host")
                or _header(request.headers, "host")
                or host
            )
        if request.is_websocket:
            scheme = {"http": "ws", "https": "wss"}.get(scheme, scheme)
        if (
            scheme not in {"http", "https", "ws", "wss"}
            or not host
            or not request.path.startswith("/")
        ):
            return None
        origin = f"{scheme}://{host}"
        parsed = urlsplit(origin)
        if not parsed.hostname or parsed.path not in {"", "/"}:
            return None
        if self.expected_public_base is not None:
            expected = urlsplit(self.expected_public_base)
            expected_scheme = (
                "wss" if request.is_websocket and expected.scheme == "https" else expected.scheme
            )
            if parsed.scheme != expected_scheme or parsed.netloc != expected.netloc:
                return None
            base_path = expected.path.rstrip("/")
            if base_path and not request.path.startswith(f"{base_path}/"):
                return None
        query = f"?{request.query_string}" if request.query_string else ""
        return f"{origin}{request.path}{query}"

    def _owned_number(self, number: str) -> Any:
        if _NUMBER_SID.fullmatch(number):
            try:
                return self._client.incoming_phone_numbers(number).fetch()
            except Exception as exc:
                _raise_carrier(exc, operation="fetch number")
        normalized = validate_e164(number)
        try:
            matches = cast(
                "list[Any]",
                self._client.incoming_phone_numbers.list(
                    phone_number=normalized,
                    limit=2,
                ),
            )
        except Exception as exc:
            _raise_carrier(exc, operation="find number")
        if len(matches) != 1:
            raise VoicekitError(
                "VK-TEL-003",
                detail=f"Twilio returned {len(matches)} owned matches for {normalized}.",
            )
        return matches[0]

    def _update_call(self, call_sid: str, **arguments: object) -> None:
        try:
            self._client.calls(_validate_call_sid(call_sid)).update(**arguments)
        except Exception as exc:
            _raise_carrier(exc, operation="update call")


def _number_info(resource: Any) -> NumberInfo:
    raw_capabilities = getattr(resource, "capabilities", {}) or {}
    capabilities = frozenset(
        str(key)
        for key, enabled in cast("dict[object, object]", raw_capabilities).items()
        if enabled
    )
    return NumberInfo(
        number=str(resource.phone_number),
        provider_id=str(resource.sid),
        friendly_name=_optional_string(getattr(resource, "friendly_name", None)),
        country=_optional_string(getattr(resource, "iso_country", None)),
        locality=_optional_string(getattr(resource, "locality", None)),
        region=_optional_string(getattr(resource, "region", None)),
        capabilities=capabilities,
    )


def _route_settings(resource: Any) -> dict[str, str | None]:
    return {field: _optional_string(getattr(resource, field, None)) for field in _ROUTE_FIELDS}


def _route_update_arguments(settings: dict[str, str | None]) -> dict[str, str]:
    return {field: value or "" for field, value in settings.items()}


def _pipecat_target(target: RuntimeTarget) -> PipecatTarget:
    if isinstance(target, LiveKitTarget):
        raise VoicekitError(
            "VK-TEL-002",
            detail="LiveKit targets must use the ledgered TwilioLiveKitSipProvisioner.",
        )
    return target


def _checked_twiml(response: VoiceResponse) -> str:
    value = str(response)
    if len(value.encode()) > 4096:
        raise VoicekitError("VK-TEL-002", detail="inline TwiML exceeds the 4KB carrier limit.")
    return value


def _amd_hold_twiml() -> str:
    response = VoiceResponse()
    response.pause(length=60)
    return _checked_twiml(response)


def _form_value(form: object | None, name: str, *, required: bool = True) -> str:
    value: object | None = None
    if form is not None:
        getter = getattr(form, "get", None)
        if callable(getter):
            value = getter(name)
    if value is None or value == "":
        if required:
            raise VoicekitError("VK-TEL-008", detail=f"carrier event lacks {name}.")
        return ""
    return str(value)


def _status_event(
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
    raise VoicekitError("VK-TEL-008", detail=f"unknown Twilio CallStatus {status!r}.")


def _validate_call_sid(value: str) -> str:
    if not _CALL_SID.fullmatch(value):
        raise VoicekitError("VK-TEL-008", detail="invalid Twilio CallSid.")
    return value


def _validate_recording_sid(value: str) -> str:
    if not _RECORDING_SID.fullmatch(value):
        raise VoicekitError("VK-TEL-008", detail="invalid Twilio RecordingSid.")
    return value


def _validate_dtmf(value: str) -> str:
    if not _DTMF.fullmatch(value):
        raise VoicekitError("VK-TEL-002", detail="DTMF digits contain unsupported characters.")
    return value


def _optional_string(value: object) -> str | None:
    return None if value in {None, ""} else str(value)


def _header(headers: dict[str, str], name: str) -> str:
    normalized = name.lower()
    return next(
        (value for key, value in headers.items() if key.lower() == normalized),
        "",
    )


def _first_header_value(value: str) -> str:
    return value.split(",", maxsplit=1)[0].strip()


def _definitive_rejection(exception: Exception) -> bool:
    return isinstance(exception, TwilioRestException) and 400 <= exception.status < 500


def _safe_carrier_status(exception: Exception) -> str:
    if isinstance(exception, TwilioRestException):
        return f"http_{exception.status}_code_{exception.code or 'unknown'}"
    return "unknown"


def _raise_carrier(exception: Exception, *, operation: str) -> NoReturn:
    if _definitive_rejection(exception):
        raise VoicekitError(
            "VK-TEL-004",
            detail=f"Twilio {operation} {_safe_carrier_status(exception)}.",
        ) from exception
    raise VoicekitError(
        "VK-TEL-011",
        detail=f"Twilio {operation} did not return a definitive result.",
    ) from exception
