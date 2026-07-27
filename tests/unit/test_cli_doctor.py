# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import email.utils
import importlib.metadata
import socket
import stat
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, cast

import httpx
import pytest

from voicekit.cli.context import ProjectContext
from voicekit.cli.doctor import (
    Doctor,
    DoctorCheck,
    _disk_check,
    _env_check,
    _port_check,
    _python_check,
    _results_endpoint,
    _runtime_check,
    _twilio_check,
)
from voicekit.cli.keys import KeyCheck
from voicekit.config.catalog import ProviderKind
from voicekit.config.manifest import ProjectManifest, RecipeSelection
from voicekit.config.models import ModelAxis
from voicekit.results.signing import WebhookSigner
from voicekit.telephony.models import CarrierAccountState, NumberInfo


class ValidKeys:
    async def validate(
        self,
        kind: ProviderKind,
        identifier: str,
        values: Mapping[str, str],
    ) -> KeyCheck:
        del kind, values
        return KeyCheck(
            provider=identifier.split("/", maxsplit=1)[0],
            env_names=("KEY",),
            status="valid",
            detail="valid",
            fix="none",
        )


class InvalidKeys:
    async def validate(
        self,
        kind: ProviderKind,
        identifier: str,
        values: Mapping[str, str],
    ) -> KeyCheck:
        del kind, values
        return KeyCheck(
            provider=identifier.split("/", maxsplit=1)[0],
            env_names=("KEY",),
            status="invalid",
            detail="rejected",
            fix="replace key",
        )


def _context(tmp_path: Path) -> ProjectContext:
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="doctor-agent",
        runtime="pipecat",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"web"}),
        models=models,
    )
    return ProjectContext(
        root=tmp_path,
        manifest=manifest,
        checkpoint=False,
        environment={
            "DEEPGRAM_API_KEY": "dg",  # pragma: allowlist secret
            "ANTHROPIC_API_KEY": "ant",  # pragma: allowlist secret
            "CARTESIA_API_KEY": "car",  # pragma: allowlist secret
        },
    )


def test_safe_fixes_are_idempotent_private_and_never_print_secret(tmp_path: Path) -> None:
    context = _context(tmp_path)
    doctor = Doctor(context, key_validator=ValidKeys())

    first = doctor.apply_safe_fixes()
    second = doctor.apply_safe_fixes()

    assert "generated VOICEKIT_WEBHOOK_SECRET" in first
    assert "generated VOICEKIT_WEBHOOK_SECRET" not in second
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / ".voicekit").stat().st_mode) == 0o700
    assert WebhookSigner(context.environment["VOICEKIT_WEBHOOK_SECRET"])
    assert "whsec_" not in " ".join(first + second)


@pytest.mark.asyncio
async def test_doctor_streams_parallel_checks_in_stable_report_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    doctor = Doctor(_context(tmp_path), key_validator=ValidKeys())
    streamed: list[str] = []

    def replacement(identifier: str) -> Callable[[], Awaitable[DoctorCheck]]:
        async def check() -> DoctorCheck:
            await asyncio.sleep(0)
            return DoctorCheck(id=identifier, description=identifier, ok=True)

        return check

    for identifier, method in (
        ("keys", "_keys"),
        ("runtime", "_runtime"),
        ("python", "_python"),
        ("audio", "_audio"),
        ("port", "_port"),
        ("tunnel", "_tunnel"),
        ("carrier", "_carrier"),
        ("livekit", "_livekit"),
        ("receiver", "_receiver"),
        ("dlq", "_dlq"),
        ("clock", "_clock"),
        ("env", "_env_diff"),
        ("disk", "_disk"),
    ):
        monkeypatch.setattr(doctor, method, replacement(identifier))

    report = await doctor.run(on_check=lambda check: streamed.append(check.id))

    assert report.ok
    assert set(streamed) == {check.id for check in report.checks}
    assert [check.id for check in report.checks] == [
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
    ]


def test_local_doctor_checks_are_actionable(tmp_path: Path) -> None:
    context = _context(tmp_path)
    (tmp_path / ".env.example").write_text(
        "DEEPGRAM_API_KEY=\nMISSING_KEY=\n",
        encoding="utf-8",
    )

    assert _python_check().ok
    assert _runtime_check(cast("ProjectManifest", context.manifest)).ok
    assert _port_check(0).ok
    assert not _env_check(context).ok
    assert "MISSING_KEY" in _env_check(context).issues[0]
    assert _disk_check(tmp_path).description.startswith("Disk")


def test_results_endpoint_parser_never_imports_project_code(tmp_path: Path) -> None:
    (tmp_path / "agent.py").write_text(
        """
raise RuntimeError("must not execute")
from voicekit import Results
results = Results(
    webhook="https://receiver.example.test/results",
    secret_env="VOICEKIT_WEBHOOK_SECRET",
)
""",
        encoding="utf-8",
    )

    assert _results_endpoint(tmp_path) == "https://receiver.example.test/results"


class FakeDoctorHttp:
    posts: ClassVar[list[str]] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return

    async def __aenter__(self) -> FakeDoctorHttp:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return

    async def head(self, url: str) -> httpx.Response:
        headers = (
            {"date": email.utils.format_datetime(datetime.now(UTC))}
            if url == "https://api.twilio.com"
            else {}
        )
        return httpx.Response(
            204,
            headers=headers,
            request=httpx.Request("HEAD", url),
        )

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes,
    ) -> httpx.Response:
        assert headers["webhook-signature"].startswith("v1,")
        assert content
        self.posts.append(url)
        return httpx.Response(204, request=httpx.Request("POST", url))


def _fake_which(_name: str) -> str:
    return "/usr/bin/ffmpeg"


@pytest.mark.asyncio
async def test_complete_web_doctor_runs_green_with_signed_receiver_test(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    doctor = Doctor(context, key_validator=ValidKeys(), send_test=True, port=_free_port())
    doctor.apply_safe_fixes()
    (tmp_path / ".env.example").write_text(
        "DEEPGRAM_API_KEY=\nANTHROPIC_API_KEY=\nCARTESIA_API_KEY=\nVOICEKIT_WEBHOOK_SECRET=\n",
        encoding="utf-8",
    )
    (tmp_path / "agent.py").write_text(
        """
from voicekit import Results
results = Results(
    webhook="https://receiver.example.test/results",
    secret_env="VOICEKIT_WEBHOOK_SECRET",
)
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("voicekit.cli.doctor.httpx.AsyncClient", FakeDoctorHttp)
    monkeypatch.setattr("voicekit.cli.doctor.shutil.which", _fake_which)

    report = await doctor.run()

    assert report.ok
    assert FakeDoctorHttp.posts == ["https://receiver.example.test/results"]
    assert all(check.advice or check.ok for check in report.checks)


class FakeCarrierLedger:
    def close(self) -> None:
        return


class FakeCarrier:
    ledger = FakeCarrierLedger()

    def __init__(self, **_kwargs: object) -> None:
        return

    def account_state(self) -> CarrierAccountState:
        return CarrierAccountState(
            provider="twilio",
            status="active",
            account_type="Trial",
            balance="0",
            currency="USD",
        )

    def list_numbers(self) -> list[NumberInfo]:
        return [
            NumberInfo(
                number="+919876543210",
                provider_id="PN123",
                country="IN",
            )
        ]

    def inbound_route(self, _number: str) -> dict[str, str | None]:
        return {"voice_url": "https://old.example.test/answer"}


def test_twilio_doctor_surfaces_trial_funding_kyc_and_route_diff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="phone-doctor",
        runtime="pipecat",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"phone"}),
        models=models,
        carriers=["twilio"],
        phone_number="+919876543210",
    )
    context = ProjectContext(
        root=tmp_path,
        manifest=manifest,
        checkpoint=False,
        environment={
            "TWILIO_ACCOUNT_SID": "AC" + "1" * 32,
            "TWILIO_AUTH_TOKEN": "token",  # pragma: allowlist secret
            "VOICEKIT_PUBLIC_URL": "https://public.example.test",
        },
    )
    monkeypatch.setattr("voicekit.telephony.twilio.TwilioAdapter", FakeCarrier)

    check = _twilio_check(context, manifest)

    assert not check.ok
    assert any("balance" in issue for issue in check.issues)
    assert any("voice_url" in issue for issue in check.issues)
    assert any("trial" in advice.casefold() for advice in check.advice)
    assert any("regulatory" in advice for advice in check.advice)


def _free_port() -> int:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = cast("int", listener.getsockname()[1])
    listener.close()
    return port


def _phone_context(tmp_path: Path, *, public_url: bool = True) -> ProjectContext:
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="phone-agent",
        runtime="pipecat",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"phone"}),
        models=models,
        carriers=["twilio"],
        phone_number="+14155550123",
    )
    environment = {
        "TWILIO_ACCOUNT_SID": "AC" + "1" * 32,
        "TWILIO_AUTH_TOKEN": "token",  # pragma: allowlist secret
    }
    if public_url:
        environment["VOICEKIT_PUBLIC_URL"] = "https://public.example.test"
    return ProjectContext(
        root=tmp_path,
        manifest=manifest,
        checkpoint=False,
        environment=environment,
    )


class ErrorScenarioHttp:
    get_status: ClassVar[int] = 503
    head_status: ClassVar[int] = 503
    raise_http: ClassVar[bool] = False
    include_date: ClassVar[bool] = False

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return

    async def __aenter__(self) -> ErrorScenarioHttp:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return

    async def get(self, url: str) -> httpx.Response:
        if self.raise_http:
            raise httpx.ConnectError("offline")
        return httpx.Response(
            self.get_status,
            request=httpx.Request("GET", url),
        )

    async def head(self, url: str) -> httpx.Response:
        if self.raise_http:
            raise httpx.ConnectError("offline")
        headers = (
            {"date": email.utils.format_datetime(datetime.now(UTC))} if self.include_date else {}
        )
        return httpx.Response(
            self.head_status,
            headers=headers,
            request=httpx.Request("HEAD", url),
        )


@pytest.mark.asyncio
async def test_doctor_failure_checks_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    web = _context(tmp_path)
    web.environment["VOICEKIT_WEBHOOK_SECRET"] = "invalid"
    invalid_keys = Doctor(web, key_validator=InvalidKeys())
    key_check = await invalid_keys._keys()
    assert not key_check.ok
    assert "not a valid whsec_" in key_check.issues[-1]

    missing_secret_context = _context(tmp_path)
    missing_secret = await Doctor(
        missing_secret_context,
        key_validator=ValidKeys(),
    )._keys()
    assert not missing_secret.ok
    assert "missing" in missing_secret.issues[-1]

    phone_without_url = Doctor(
        _phone_context(tmp_path, public_url=False),
        key_validator=ValidKeys(),
    )
    assert not (await phone_without_url._tunnel()).ok

    ErrorScenarioHttp.get_status = 503
    ErrorScenarioHttp.raise_http = False
    monkeypatch.setattr("voicekit.cli.doctor.httpx.AsyncClient", ErrorScenarioHttp)
    phone = Doctor(_phone_context(tmp_path), key_validator=ValidKeys())
    status_failure = await phone._tunnel()
    assert not status_failure.ok
    assert "503" in status_failure.issues[0]

    ErrorScenarioHttp.raise_http = True
    unreachable = await phone._tunnel()
    assert not unreachable.ok
    assert "unreachable" in unreachable.issues[0]


@pytest.mark.asyncio
async def test_receiver_clock_livekit_and_dlq_failure_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    doctor = Doctor(context, key_validator=ValidKeys())
    assert not (await doctor._receiver()).ok

    (tmp_path / "agent.py").write_text(
        """
from voicekit import Results
results = Results(
    webhook="https://receiver.example.test/results",
    secret_env="VOICEKIT_WEBHOOK_SECRET",
)
""",
        encoding="utf-8",
    )
    ErrorScenarioHttp.head_status = 503
    ErrorScenarioHttp.raise_http = False
    ErrorScenarioHttp.include_date = False
    monkeypatch.setattr("voicekit.cli.doctor.httpx.AsyncClient", ErrorScenarioHttp)
    receiver = await doctor._receiver()
    assert not receiver.ok
    assert "503" in receiver.issues[0]
    assert not (await doctor._clock()).ok

    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    livekit_manifest = ProjectManifest(
        project_name="livekit-agent",
        runtime="livekit",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"web"}),
        models=models,
    )
    livekit = Doctor(
        ProjectContext(tmp_path, livekit_manifest, False, {}),
        key_validator=ValidKeys(),
    )
    assert not (await livekit._livekit()).ok

    class FakeRepository:
        def __init__(self, _path: Path) -> None:
            return

        async def __aenter__(self) -> FakeRepository:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return

        async def dlq_depth(self) -> int:
            return 2

    database = tmp_path / ".voicekit" / "calls.sqlite3"
    database.parent.mkdir()
    database.touch()
    monkeypatch.setattr("voicekit.cli.doctor.SQLiteRepository", FakeRepository)
    dlq = await doctor._dlq()
    assert not dlq.ok
    assert "2 delivery" in dlq.issues[0]


def test_doctor_local_negative_branches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    assert not _env_check(context).ok

    (tmp_path / "agent.py").write_text("this is not valid Python !", encoding="utf-8")
    assert _results_endpoint(tmp_path) is None
    (tmp_path / "agent.py").unlink()
    assert _results_endpoint(tmp_path) is None

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    occupied = cast("int", listener.getsockname()[1])
    try:
        assert not _port_check(occupied).ok
    finally:
        listener.close()

    def missing_package(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr("voicekit.cli.doctor.importlib.metadata.version", missing_package)
    assert not _runtime_check(cast("ProjectManifest", context.manifest)).ok

    invalid_manifest = cast(
        "ProjectManifest",
        context.manifest,
    ).model_copy(
        update={
            "channels": frozenset({"phone"}),
            "carriers": ["telnyx"],
            "phone_number": "+14155550123",
        }
    )
    invalid_carrier = _twilio_check(
        ProjectContext(tmp_path, invalid_manifest, False, {}),
        invalid_manifest,
    )
    assert not invalid_carrier.ok
