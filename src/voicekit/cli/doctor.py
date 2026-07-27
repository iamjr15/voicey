"""Parallel production preflight checks for local P1 projects."""

from __future__ import annotations

import ast
import asyncio
import email.utils
import importlib.metadata
import json
import secrets
import shutil
import socket
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import httpx

from voicekit.cli.context import ProjectContext, require_manifest
from voicekit.cli.environment import EnvFileStore, ensure_env_ignored
from voicekit.cli.keys import (
    LIVEKIT_ENV_VARS,
    KeyValidator,
    LiveKitKeyValidator,
    ProviderKeyValidator,
    RuntimeKeyValidator,
    required_entries,
)
from voicekit.config.manifest import ProjectManifest
from voicekit.errors import VoicekitError
from voicekit.results.signing import WebhookSigner, encode_secret
from voicekit.storage.sqlite import SQLiteRepository
from voicekit.telephony.models import PipecatTarget


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    id: str
    description: str
    ok: bool
    issues: tuple[str, ...] = ()
    advice: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


CheckCallback = Callable[[DoctorCheck], None]
DoctorCheckFactory = Callable[[], Awaitable[DoctorCheck]]


class LiveKitSipInspector:
    """Read current SIP resources without mutating the LiveKit project."""

    async def inspect(
        self,
        values: Mapping[str, str],
        *,
        expected_name: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        from livekit import api

        client: api.LiveKitAPI | None = None
        try:
            client = api.LiveKitAPI(
                url=values["LIVEKIT_URL"],
                api_key=values["LIVEKIT_API_KEY"],
                api_secret=values["LIVEKIT_API_SECRET"],
            )
            inbound, outbound, dispatch = await asyncio.gather(
                client.sip.list_sip_inbound_trunk(api.ListSIPInboundTrunkRequest()),
                client.sip.list_sip_outbound_trunk(api.ListSIPOutboundTrunkRequest()),
                client.sip.list_sip_dispatch_rule(api.ListSIPDispatchRuleRequest()),
            )
        except Exception:
            return (
                ("LiveKit SIP trunk and dispatch reads were unreachable or rejected.",),
                ("Check project permissions and network, then rerun `voicekit doctor`.",),
            )
        finally:
            if client is not None:
                await client.aclose()
        missing: list[str] = []
        if not any(item.name in {expected_name, f"{expected_name}-in"} for item in inbound.items):
            missing.append("inbound trunk")
        if not any(item.name in {expected_name, f"{expected_name}-out"} for item in outbound.items):
            missing.append("outbound trunk")
        if not any(item.name == expected_name for item in dispatch.items):
            missing.append("dispatch rule")
        if not missing:
            return (), ()
        return (
            (f"LiveKit is missing {', '.join(missing)} named {expected_name!r}.",),
            (
                "Run `voicekit dev --phone` for a temporary provisioned route "
                "or provision the documented persistent SIP chain.",
            ),
        )


class Doctor:
    """Run independent checks concurrently and stream each completed result."""

    def __init__(
        self,
        context: ProjectContext,
        *,
        key_validator: KeyValidator | None = None,
        livekit_validator: RuntimeKeyValidator | None = None,
        livekit_sip_inspector: LiveKitSipInspector | None = None,
        port: int = 7860,
        send_test: bool = False,
    ) -> None:
        if not 1 <= port <= 65535:
            raise VoicekitError("VK-CLI-006", detail="doctor port must be from 1 through 65535.")
        self.context = context
        self.manifest = require_manifest(context)
        self.key_validator = key_validator or ProviderKeyValidator()
        self.livekit_validator = livekit_validator or LiveKitKeyValidator()
        self.livekit_sip_inspector = livekit_sip_inspector or LiveKitSipInspector()
        self.port = port
        self.send_test = send_test

    async def run(self, *, on_check: CheckCallback | None = None) -> DoctorReport:
        factories: tuple[DoctorCheckFactory, ...] = (
            self._keys,
            self._runtime,
            self._python,
            self._audio,
            self._port,
            self._tunnel,
            self._carrier,
            self._livekit,
            self._receiver,
            self._dlq,
            self._clock,
            self._env_diff,
            self._disk,
        )
        tasks = [asyncio.create_task(factory()) for factory in factories]
        completed: list[DoctorCheck] = []
        for task in asyncio.as_completed(tasks):
            check = await task
            completed.append(check)
            if on_check is not None:
                on_check(check)
        by_id = {check.id: check for check in completed}
        return DoctorReport(checks=tuple(by_id[check_id] for check_id in _CHECK_ORDER))

    def apply_safe_fixes(self) -> tuple[str, ...]:
        fixed: list[str] = []
        ensure_env_ignored(self.context.root)
        fixed.append("ensured .env* is ignored")
        env_store = EnvFileStore(self.context.root / ".env")
        values = env_store.read()
        if not values.get("VOICEKIT_WEBHOOK_SECRET") and not self.context.environment.get(
            "VOICEKIT_WEBHOOK_SECRET"
        ):
            env_store.update({"VOICEKIT_WEBHOOK_SECRET": encode_secret(secrets.token_bytes(32))})
            self.context.environment.update(env_store.read())
            fixed.append("generated VOICEKIT_WEBHOOK_SECRET")
        data_dir = self.context.root / ".voicekit"
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        data_dir.chmod(0o700)
        fixed.append("secured .voicekit directory")
        return tuple(fixed)

    async def _keys(self) -> DoctorCheck:
        issues: list[str] = []
        advice: list[str] = []
        for entry in required_entries(
            cast("dict[str, str]", self.manifest.models),
            carrier=self.manifest.carriers[0] if self.manifest.carriers else None,
        ):
            check = await self.key_validator.validate(
                entry.kind,
                entry.id,
                self.context.environment,
            )
            if check.status != "valid":
                issues.append(f"{check.provider}: {check.detail}")
                advice.append(check.fix)
        secret = self.context.environment.get("VOICEKIT_WEBHOOK_SECRET")
        if not secret:
            issues.append("VOICEKIT_WEBHOOK_SECRET is missing.")
            advice.append("Run `voicekit doctor --fix`.")
        else:
            try:
                WebhookSigner(secret)
            except VoicekitError:
                issues.append("VOICEKIT_WEBHOOK_SECRET is not a valid whsec_ value.")
                advice.append("Run `voicekit doctor --fix` after removing the invalid value.")
        return _result("keys", "Provider keys and webhook signing secret", issues, advice)

    async def _runtime(self) -> DoctorCheck:
        return await asyncio.to_thread(_runtime_check, self.manifest)

    async def _python(self) -> DoctorCheck:
        return await asyncio.to_thread(_python_check)

    async def _audio(self) -> DoctorCheck:
        return await asyncio.to_thread(_audio_check)

    async def _port(self) -> DoctorCheck:
        return await asyncio.to_thread(_port_check, self.port)

    async def _tunnel(self) -> DoctorCheck:
        if "phone" not in self.manifest.channels:
            return _result(
                "tunnel",
                "Public tunnel reachability",
                [],
                ["Not required for this web-only project."],
            )
        public_url = self.context.environment.get("VOICEKIT_PUBLIC_URL")
        if not public_url:
            return _result(
                "tunnel",
                "Public tunnel reachability",
                ["VOICEKIT_PUBLIC_URL is absent."],
                ["Run `voicekit dev --phone` to create and probe a tunnel."],
            )
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                response = await client.get(f"{public_url.rstrip('/')}/health")
            issues = (
                []
                if response.status_code < 500
                else [f"health returned HTTP {response.status_code}."]
            )
        except httpx.HTTPError:
            issues = ["The configured public tunnel is unreachable."]
        return _result(
            "tunnel",
            "Public tunnel reachability",
            issues,
            ["Restart `voicekit dev --phone` and use its current public URL."] if issues else [],
        )

    async def _carrier(self) -> DoctorCheck:
        if "phone" not in self.manifest.channels:
            return _result(
                "carrier",
                "Carrier account, funding, KYC, and inbound route",
                [],
                ["Not required for this web-only project."],
            )
        return await asyncio.to_thread(
            _carrier_check,
            self.context,
            self.manifest,
        )

    async def _livekit(self) -> DoctorCheck:
        if self.manifest.runtime != "livekit":
            return _result(
                "livekit",
                "LiveKit project, SIP trunk, and dispatch",
                [],
                ["Not required for the Pipecat runtime."],
            )
        issues: list[str] = []
        advice: list[str] = []
        check = await self.livekit_validator.validate(self.context.environment)
        if check.status != "valid":
            issues.append(check.detail)
            advice.append(check.fix)
        if "phone" in self.manifest.channels:
            if self.manifest.carriers == ["telnyx"]:
                sip_names = (
                    "VOICEKIT_LIVEKIT_SIP_URI",
                    "VOICEKIT_TELNYX_SIP_USERNAME",
                    "VOICEKIT_TELNYX_SIP_PASSWORD",
                )
                carrier_label = "Telnyx↔LiveKit"
            elif self.manifest.carriers == ["vobiz"]:
                sip_names = (
                    "VOICEKIT_LIVEKIT_SIP_URI",
                    "VOICEKIT_VOBIZ_SIP_CREDENTIAL_ID",
                    "VOICEKIT_VOBIZ_SIP_USERNAME",
                    "VOICEKIT_VOBIZ_SIP_PASSWORD",
                )
                carrier_label = "Vobiz↔LiveKit"
            elif self.manifest.carriers == ["plivo"]:
                sip_names = (
                    "VOICEKIT_LIVEKIT_SIP_URI",
                    "VOICEKIT_PLIVO_SIP_USERNAME",
                    "VOICEKIT_PLIVO_SIP_PASSWORD",
                )
                carrier_label = "Plivo↔LiveKit"
            elif self.manifest.carriers == ["sip"]:
                sip_names = (
                    "VOICEKIT_SIP_ADDRESS",
                    "VOICEKIT_SIP_USERNAME",
                    "VOICEKIT_SIP_PASSWORD",
                    "VOICEKIT_SIP_TRANSPORT",
                    "VOICEKIT_SIP_MEDIA_ENCRYPTION",
                )
                carrier_label = "generic SIP↔LiveKit"
            else:
                sip_names = (
                    "VOICEKIT_LIVEKIT_SIP_URI",
                    "VOICEKIT_TWILIO_SIP_DOMAIN",
                    "VOICEKIT_TWILIO_SIP_USERNAME",
                    "VOICEKIT_TWILIO_SIP_PASSWORD",
                )
                carrier_label = "Twilio↔LiveKit"
            missing = [name for name in sip_names if not self.context.environment.get(name)]
            if missing:
                issues.append(f"Missing LiveKit SIP provisioning values: {', '.join(missing)}.")
                advice.append(f"Add the {carrier_label} SIP values, then rerun `voicekit doctor`.")
            elif check.status == "valid" and self.manifest.phone_number is not None:
                digits = self.manifest.phone_number.removeprefix("+")
                sip_issues, sip_advice = await self.livekit_sip_inspector.inspect(
                    self.context.environment,
                    expected_name=f"voicekit-{self.manifest.project_name}-{digits}",
                )
                issues.extend(sip_issues)
                advice.extend(sip_advice)
        else:
            advice.append("SIP trunk and dispatch are not required for this web-only project.")
        if any(not self.context.environment.get(name) for name in LIVEKIT_ENV_VARS):
            advice.append("Run `voicekit keys add livekit`.")
        return _result(
            "livekit",
            "LiveKit project, SIP trunk, and dispatch",
            issues,
            advice,
        )

    async def _receiver(self) -> DoctorCheck:
        endpoint = await asyncio.to_thread(_results_endpoint, self.context.root)
        if endpoint is None or urlsplit(endpoint).hostname == "example.invalid":
            return _result(
                "receiver",
                "Results receiver and Standard Webhooks round-trip",
                ["A production results receiver is not configured."],
                ["Set Results.webhook in agent.py to your HTTPS receiver before deployment."],
            )
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                if self.send_test:
                    secret = self.context.environment.get("VOICEKIT_WEBHOOK_SECRET", "")
                    body = json.dumps(
                        {"type": "voicekit.doctor.test"},
                        separators=(",", ":"),
                    ).encode()
                    signed = WebhookSigner(secret).sign("evt_doctor_test", body)
                    response = await client.post(
                        endpoint,
                        headers={**signed.headers, "content-type": "application/json"},
                        content=body,
                    )
                else:
                    response = await client.head(endpoint)
            issues = (
                []
                if response.status_code < 500
                else [f"receiver returned HTTP {response.status_code}."]
            )
        except (httpx.HTTPError, VoicekitError):
            issues = ["The configured results receiver is unreachable or rejected the signed test."]
        advice = (
            ["Start the receiver and verify its raw-body Standard Webhooks handler."]
            if issues
            else (
                ["Reachability checked; pass --send-test for a signed POST round-trip."]
                if not self.send_test
                else []
            )
        )
        return _result(
            "receiver",
            "Results receiver and Standard Webhooks round-trip",
            issues,
            advice,
        )

    async def _dlq(self) -> DoctorCheck:
        path = self.context.root / ".voicekit" / "calls.sqlite3"
        if not path.exists():
            return _result("dlq", "Dead-letter queue depth", [], ["No calls recorded yet."])
        async with SQLiteRepository(path) as repository:
            depth = await repository.dlq_depth()
        issues = [] if depth == 0 else [f"{depth} delivery event(s) are dead-lettered."]
        advice = ["Run `voicekit calls list --undelivered`."] if issues else []
        return _result("dlq", "Dead-letter queue depth", issues, advice)

    async def _clock(self) -> DoctorCheck:
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                response = await client.head("https://api.twilio.com")
            date = response.headers.get("date")
            parsed = None if date is None else email.utils.parsedate_to_datetime(date)
            if parsed is None:
                raise ValueError("Date header absent")
            skew = abs((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds())
            issues = [] if skew <= 60 else [f"system clock differs by {skew:.0f}s."]
        except (httpx.HTTPError, TypeError, ValueError):
            issues = ["Could not compare the system clock to an HTTPS provider."]
        return _result(
            "clock",
            "Clock skew for signatures",
            issues,
            ["Enable automatic time synchronization, then rerun doctor."] if issues else [],
        )

    async def _env_diff(self) -> DoctorCheck:
        return await asyncio.to_thread(_env_check, self.context)

    async def _disk(self) -> DoctorCheck:
        return await asyncio.to_thread(_disk_check, self.context.root)


_CHECK_ORDER = (
    "keys",
    "runtime",
    "python",
    "audio",
    "port",
    "tunnel",
    "carrier",
    "livekit",
    "receiver",
    "dlq",
    "clock",
    "env",
    "disk",
)


def _result(
    identifier: str,
    description: str,
    issues: list[str],
    advice: list[str],
) -> DoctorCheck:
    return DoctorCheck(
        id=identifier,
        description=description,
        ok=not issues,
        issues=tuple(issues),
        advice=tuple(dict.fromkeys(advice)),
    )


def _runtime_check(manifest: ProjectManifest) -> DoctorCheck:
    package, expected = (
        ("pipecat-ai", "1.6.0") if manifest.runtime == "pipecat" else ("livekit-agents", "1.6.7")
    )
    try:
        installed = importlib.metadata.version(package)
        issues = (
            [] if installed == expected else [f"{package}=={installed}; tested pin is {expected}."]
        )
    except importlib.metadata.PackageNotFoundError:
        issues = [f"{package} is not installed."]
    extra = manifest.runtime
    return _result(
        "runtime",
        "Runtime package version",
        issues,
        [f'Run `uv pip install "voicekit[{extra}]"`.'] if issues else [],
    )


def _python_check() -> DoctorCheck:
    version = sys.version_info
    issues = (
        []
        if (3, 11) <= version[:2] < (3, 15)
        else [f"Python {version.major}.{version.minor} is unsupported."]
    )
    return _result(
        "python",
        "Python 3.11-3.14",
        issues,
        ["Use Python 3.11, 3.12, 3.13, or 3.14."] if issues else [],
    )


def _audio_check() -> DoctorCheck:
    issues = [] if shutil.which("ffmpeg") else ["ffmpeg is not on PATH."]
    return _result(
        "audio",
        "Audio dependency (ffmpeg)",
        issues,
        ["Install ffmpeg with your operating-system package manager."] if issues else [],
    )


def _port_check(port: int) -> DoctorCheck:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        issues: list[str] = []
    except OSError:
        issues = [f"127.0.0.1:{port} is already in use."]
    finally:
        sock.close()
    return _result(
        "port",
        f"Local port {port}",
        issues,
        [f"Stop the process on port {port} or pass --port to doctor/dev."] if issues else [],
    )


def _carrier_check(context: ProjectContext, manifest: ProjectManifest) -> DoctorCheck:
    if manifest.carriers == ["telnyx"]:
        return _telnyx_check(context, manifest)
    if manifest.carriers == ["vobiz"]:
        return _vobiz_check(context, manifest)
    if manifest.carriers == ["plivo"]:
        return _plivo_check(context, manifest)
    if manifest.carriers == ["sip"]:
        return _generic_sip_check(context, manifest)
    return _twilio_check(context, manifest)


def _twilio_check(context: ProjectContext, manifest: ProjectManifest) -> DoctorCheck:
    from voicekit.telephony.twilio import TwilioAdapter

    if manifest.carriers != ["twilio"] or manifest.phone_number is None:
        return _result(
            "carrier",
            "Carrier account, funding, KYC, and inbound route",
            ["P1 doctor supports the enabled Twilio carrier only."],
            ["Resume init with Twilio or install the phase that supports this carrier."],
        )
    issues: list[str] = []
    advice: list[str] = []
    adapter = TwilioAdapter(
        account_sid=context.environment.get("TWILIO_ACCOUNT_SID"),
        auth_token=context.environment.get("TWILIO_AUTH_TOKEN"),
        ledger_path=context.root / ".voicekit" / "telephony.sqlite3",
        expected_public_base=context.environment.get("VOICEKIT_PUBLIC_URL"),
    )
    try:
        account = adapter.account_state()
        if account.status.casefold() != "active":
            issues.append(f"Twilio account status is {account.status}.")
            advice.append("Resolve the account status in the Twilio Console.")
        if account.account_type and account.account_type.casefold() == "trial":
            advice.append(
                "Twilio trial calls include a preamble and can dial verified numbers only; "
                "upgrade the project to remove those limits."
            )
        try:
            balance = None if account.balance is None else float(account.balance)
        except ValueError:
            balance = None
        if balance is not None and balance <= 0:
            issues.append(f"Twilio balance is {account.balance} {account.currency or ''}.".strip())
            advice.append("Fund the Twilio project before placing calls.")
        numbers = adapter.list_numbers()
        selected = [number for number in numbers if number.number == manifest.phone_number]
        if len(selected) != 1:
            issues.append(f"{manifest.phone_number} is not uniquely owned by this Twilio account.")
            advice.append("Select an owned E.164 number with `voicekit init --resume`.")
        elif selected[0].country not in {None, "US", "CA"}:
            advice.append(
                f"{selected[0].country} numbers may require an approved regulatory bundle; "
                "verify the number's Twilio compliance status before production."
            )
        public_url = context.environment.get("VOICEKIT_PUBLIC_URL")
        if public_url:
            expected = PipecatTarget(public_url).answer_url
            route = adapter.inbound_route(manifest.phone_number)
            if route["voice_url"] != expected:
                issues.append(f"Twilio voice_url is {route['voice_url']!r}; expected {expected!r}.")
                advice.append("Run `voicekit numbers point --yes` or `voicekit dev --phone`.")
        else:
            issues.append(
                "VOICEKIT_PUBLIC_URL is absent, so the live Twilio route cannot be diffed."
            )
            advice.append("Run `voicekit dev --phone`.")
    finally:
        adapter.ledger.close()
    return _result(
        "carrier",
        "Carrier account, funding, KYC, and inbound route",
        issues,
        advice,
    )


def _telnyx_check(context: ProjectContext, manifest: ProjectManifest) -> DoctorCheck:
    from voicekit.telephony.telnyx import TelnyxAdapter

    if manifest.carriers != ["telnyx"] or manifest.phone_number is None:
        return _result(
            "carrier",
            "Carrier account, funding, KYC, and inbound route",
            ["The selected carrier is not available in this certification phase."],
            ["Resume init with Twilio or Telnyx, then rerun `voicekit doctor`."],
        )
    required = ("TELNYX_API_KEY", "TELNYX_PUBLIC_KEY", "TELNYX_CONNECTION_ID")
    missing = [name for name in required if not context.environment.get(name)]
    if missing:
        return _result(
            "carrier",
            "Telnyx account, funding, signatures, and inbound route",
            [f"Missing Telnyx values: {', '.join(missing)}."],
            ["Add the Telnyx API key, public key, and Voice API connection id."],
        )
    issues: list[str] = []
    advice: list[str] = []
    adapter = TelnyxAdapter(
        api_key=context.environment.get("TELNYX_API_KEY"),
        public_key=context.environment.get("TELNYX_PUBLIC_KEY"),
        connection_id=context.environment.get("TELNYX_CONNECTION_ID"),
        ledger_path=context.root / ".voicekit" / "telephony.sqlite3",
    )
    try:
        account = adapter.account_state()
        try:
            balance = None if account.balance is None else float(account.balance)
        except ValueError:
            balance = None
        if balance is not None and balance <= 0:
            issues.append(f"Telnyx balance is {account.balance} {account.currency or ''}.".strip())
            advice.append("Fund the paid Telnyx account before placing calls.")
        numbers = adapter.list_numbers()
        selected = [number for number in numbers if number.number == manifest.phone_number]
        if len(selected) != 1:
            issues.append(f"{manifest.phone_number} is not uniquely owned by this Telnyx account.")
            advice.append("Select an owned E.164 number with `voicekit init --resume`.")
        else:
            route = adapter.inbound_route(manifest.phone_number)
            expected = context.environment.get("TELNYX_CONNECTION_ID")
            if route.get("connection_id") != expected:
                issues.append("The selected Telnyx number is not assigned to the configured app.")
                advice.append("Run `voicekit dev --phone` for a temporary conflict-safe route.")
        advice.append(
            "Telnyx LiveKit SIP requires a paid account; confirm KYC and destination permissions."
        )
    finally:
        adapter.ledger.close()
    return _result(
        "carrier",
        "Telnyx account, funding, signatures, and inbound route",
        issues,
        advice,
    )


def _vobiz_check(context: ProjectContext, manifest: ProjectManifest) -> DoctorCheck:
    from voicekit.telephony.vobiz import VobizAdapter

    if manifest.carriers != ["vobiz"] or manifest.phone_number is None:
        return _result(
            "carrier",
            "Carrier account, funding, KYC, and inbound route",
            ["The selected carrier is not available in this certification phase."],
            ["Resume init with Twilio, Telnyx, or Vobiz, then rerun `voicekit doctor`."],
        )
    required = ("VOBIZ_AUTH_ID", "VOBIZ_AUTH_TOKEN")
    missing = [name for name in required if not context.environment.get(name)]
    if missing:
        return _result(
            "carrier",
            "Vobiz account, funding, signatures, and inbound route",
            [f"Missing Vobiz values: {', '.join(missing)}."],
            ["Add the Vobiz Auth ID and Auth Token, then rerun `voicekit doctor`."],
        )
    issues: list[str] = []
    advice: list[str] = []
    adapter = VobizAdapter(
        auth_id=context.environment.get("VOBIZ_AUTH_ID"),
        auth_token=context.environment.get("VOBIZ_AUTH_TOKEN"),
        ledger_path=context.root / ".voicekit" / "telephony.sqlite3",
        expected_public_base=context.environment.get("VOICEKIT_PUBLIC_URL"),
    )
    try:
        account = adapter.account_state()
        try:
            balance = None if account.balance is None else float(account.balance)
        except ValueError:
            balance = None
        if balance is not None and balance <= 0:
            issues.append(f"Vobiz balance is {account.balance} {account.currency or ''}.".strip())
            advice.append("Fund the Vobiz account before placing calls.")
        numbers = adapter.list_numbers()
        selected = [number for number in numbers if number.number == manifest.phone_number]
        if len(selected) != 1:
            issues.append(f"{manifest.phone_number} is not uniquely owned by this Vobiz account.")
            advice.append("Select an owned E.164 number with `voicekit init --resume`.")
        else:
            route = adapter.inbound_route(manifest.phone_number)
            if not route.get("application_id") and not route.get("trunk_group_id"):
                issues.append("The selected Vobiz number has no inbound application or SIP trunk.")
                advice.append("Run `voicekit dev --phone` for a temporary conflict-safe route.")
        advice.append(
            "Confirm Vobiz account KYC and destination permissions before live PSTN tests."
        )
    finally:
        adapter.ledger.close()
    return _result(
        "carrier",
        "Vobiz account, funding, signatures, and inbound route",
        issues,
        advice,
    )


def _plivo_check(context: ProjectContext, manifest: ProjectManifest) -> DoctorCheck:
    from voicekit.telephony.plivo import PlivoAdapter

    if manifest.phone_number is None:
        return _result(
            "carrier",
            "Plivo beta account, funding, signatures, and inbound route",
            ["The Plivo project has no selected number."],
            ["Resume init and select one owned E.164 number."],
        )
    required = ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN")
    missing = [name for name in required if not context.environment.get(name)]
    if missing:
        return _result(
            "carrier",
            "Plivo beta account, funding, signatures, and inbound route",
            [f"Missing Plivo values: {', '.join(missing)}."],
            ["Run `voicekit keys add plivo`, then rerun `voicekit doctor`."],
        )
    issues: list[str] = []
    advice: list[str] = []
    adapter = PlivoAdapter(
        auth_id=context.environment.get("PLIVO_AUTH_ID"),
        auth_token=context.environment.get("PLIVO_AUTH_TOKEN"),
        ledger_path=context.root / ".voicekit" / "telephony.sqlite3",
        expected_public_base=context.environment.get("VOICEKIT_PUBLIC_URL"),
    )
    try:
        account = adapter.account_state()
        try:
            balance = None if account.balance is None else float(account.balance)
        except ValueError:
            balance = None
        if balance is not None and balance <= 0:
            issues.append(f"Plivo balance is {account.balance} {account.currency or ''}.".strip())
            advice.append("Fund the Plivo account before placing a paid call.")
        selected = [
            number for number in adapter.list_numbers() if number.number == manifest.phone_number
        ]
        if len(selected) != 1:
            issues.append(f"{manifest.phone_number} is not uniquely owned by this Plivo account.")
            advice.append("Select an owned E.164 number with `voicekit init --resume`.")
        elif manifest.runtime == "pipecat":
            route = adapter.inbound_route(manifest.phone_number)
            if not route.get("app_id"):
                issues.append("The selected Plivo number has no inbound application.")
                advice.append("Run `voicekit dev --phone` for a conflict-safe temporary route.")
        advice.append(
            "Plivo remains Beta: run the documented inbound/outbound PSTN checklist "
            "before production traffic."
        )
    finally:
        adapter.ledger.close()
    return _result(
        "carrier",
        "Plivo beta account, funding, signatures, and inbound route",
        issues,
        advice,
    )


def _generic_sip_check(context: ProjectContext, manifest: ProjectManifest) -> DoctorCheck:
    required = (
        "VOICEKIT_SIP_ADDRESS",
        "VOICEKIT_SIP_USERNAME",
        "VOICEKIT_SIP_PASSWORD",
        "VOICEKIT_SIP_TRANSPORT",
        "VOICEKIT_SIP_MEDIA_ENCRYPTION",
    )
    missing = [name for name in required if not context.environment.get(name)]
    issues = ["Generic SIP is LiveKit-only."] if manifest.runtime != "livekit" else []
    advice = ["Resume init with the LiveKit runtime."] if manifest.runtime != "livekit" else []
    if missing:
        issues.append(f"Missing generic SIP values: {', '.join(missing)}.")
        advice.append("Run `voicekit keys add sip`, then rerun `voicekit doctor`.")
    advice.append(
        "Generic SIP is Beta and operator-managed: point the PBX/carrier at the "
        "LiveKit SIP endpoint, then complete the documented loopback checklist."
    )
    return _result(
        "carrier",
        "Generic SIP beta configuration and operator route",
        issues,
        advice,
    )


def _results_endpoint(root: Path) -> str | None:
    path = root / "agent.py"
    if not path.exists():
        return None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name != "Results":
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "webhook"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                return keyword.value.value
    return None


def _env_check(context: ProjectContext) -> DoctorCheck:
    path = context.root / ".env.example"
    if not path.exists():
        return _result(
            "env",
            ".env and .env.example agreement",
            [".env.example is missing."],
            ["Resume init to restore the generated environment template."],
        )
    expected = set(EnvFileStore(path).read())
    missing = sorted(name for name in expected if not context.environment.get(name))
    issues = [] if not missing else [f"Missing values: {', '.join(missing)}."]
    return _result(
        "env",
        ".env and .env.example agreement",
        issues,
        ["Run the matching `voicekit keys add <provider>` command."] if issues else [],
    )


def _disk_check(root: Path) -> DoctorCheck:
    free = shutil.disk_usage(root).free
    minimum = 1_000_000_000
    issues = [] if free >= minimum else [f"Only {free / 1_000_000:.0f} MB is free."]
    return _result(
        "disk",
        "Disk space for recordings and dead letters",
        issues,
        ["Free at least 1 GB before accepting recorded calls."] if issues else [],
    )
