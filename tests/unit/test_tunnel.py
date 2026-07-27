import asyncio
from types import ModuleType
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import ServerConnection, serve

from voicekit.errors import VoicekitError
from voicekit.tunnel import TunnelManager, TunnelProbe


class _Listener:
    def __init__(self, url: str = "https://voicekit-test.ngrok.app") -> None:
        self._url = url
        self.close_calls = 0

    def url(self) -> str:
        return self._url

    async def close(self) -> None:
        self.close_calls += 1


class _Process:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
        exit_on_terminate: bool = True,
    ) -> None:
        self._stdout = asyncio.StreamReader()
        self._stderr = asyncio.StreamReader()
        self._stdout.feed_data(stdout.encode())
        self._stdout.feed_eof()
        self._stderr.feed_data(stderr.encode())
        self._stderr.feed_eof()
        self._returncode = returncode
        self._exit_on_terminate = exit_on_terminate
        self._finished = asyncio.Event()
        if returncode is not None:
            self._finished.set()
        self.terminate_calls = 0
        self.kill_calls = 0

    @property
    def stdout(self) -> asyncio.StreamReader:
        return self._stdout

    @property
    def stderr(self) -> asyncio.StreamReader:
        return self._stderr

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self._exit_on_terminate:
            self._returncode = 0
            self._finished.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self._returncode = -9
        self._finished.set()

    async def wait(self) -> int:
        await self._finished.wait()
        assert self._returncode is not None
        return self._returncode


class _ProcessFactory:
    def __init__(self, process: _Process) -> None:
        self.process = process
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def __call__(
        self,
        *args: object,
        **kwargs: object,
    ) -> asyncio.subprocess.Process:
        self.calls.append((args, kwargs))
        return cast(asyncio.subprocess.Process, self.process)


def _ngrok_module(
    listener: _Listener,
    captured: dict[str, object],
) -> ModuleType:
    module = ModuleType("ngrok")

    def forward(address: str, **options: object) -> _Listener:
        captured["address"] = address
        captured["options"] = options
        return listener

    module.__dict__["forward"] = forward
    return module


def test_auto_resolution_prefers_ngrok_only_when_token_exists() -> None:
    assert TunnelManager(environment={"NGROK_AUTHTOKEN": "configured"}).resolve() == "ngrok"
    assert TunnelManager(environment={}).resolve() == "cloudflared"
    assert TunnelManager(environment={}).resolve("url") == "url"
    assert TunnelManager(environment={}).resolve("ngrok") == "ngrok"


async def test_ngrok_sdk_forward_and_close_are_exact_and_idempotent() -> None:
    listener = _Listener()
    captured: dict[str, object] = {}
    manager = TunnelManager(
        environment={"NGROK_AUTHTOKEN": "test-token"},
        ngrok_module=_ngrok_module(listener, captured),
    )

    handle = await manager.open(7860)
    await asyncio.gather(handle.close(), handle.close())

    assert handle.provider == "ngrok"
    assert handle.public_url == "https://voicekit-test.ngrok.app"
    assert handle.websocket_base == "wss://voicekit-test.ngrok.app"
    assert captured == {
        "address": "http://127.0.0.1:7860",
        "options": {"authtoken": "test-token"},
    }
    assert listener.close_calls == 1
    assert handle.closed


async def test_ngrok_invalid_public_url_closes_listener() -> None:
    listener = _Listener("http://unsafe.ngrok.app")
    manager = TunnelManager(
        environment={"NGROK_AUTHTOKEN": "test-token"},
        ngrok_module=_ngrok_module(listener, {}),
    )

    with pytest.raises(VoicekitError, match="VK-TUN-002"):
        await manager.open(7860)

    assert listener.close_calls == 1


async def test_cloudflared_uses_exec_parses_stderr_and_terminates() -> None:
    process = _Process(
        stderr=(
            "INF Requesting new quick Tunnel\nINF https://safe-test.trycloudflare.com is ready\n"
        )
    )
    factory = _ProcessFactory(process)
    manager = TunnelManager(
        environment={},
        which=lambda _name: "/usr/local/bin/cloudflared",
        process_factory=factory,
    )

    handle = await manager.open(
        7860,
        preference="cloudflared",
        cloudflared_protocol="http2",
    )
    await handle.close()

    arguments, options = factory.calls[0]
    assert arguments == (
        "/usr/local/bin/cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--url",
        "http://127.0.0.1:7860",
        "--protocol",
        "http2",
    )
    assert options["stdout"] == asyncio.subprocess.PIPE
    assert options["stderr"] == asyncio.subprocess.PIPE
    assert handle.public_url == "https://safe-test.trycloudflare.com"
    assert process.terminate_calls == 1
    assert process.kill_calls == 0


async def test_cloudflared_escalates_from_terminate_to_kill() -> None:
    process = _Process(
        stderr="https://forced-stop.trycloudflare.com\n",
        exit_on_terminate=False,
    )
    manager = TunnelManager(
        environment={},
        which=lambda _name: "/opt/cloudflared",
        process_factory=_ProcessFactory(process),
    )
    handle = await manager.open(
        9000,
        preference="cloudflared",
        shutdown_timeout_s=0.001,
    )

    await handle.close()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.returncode == -9


async def test_cloudflared_failure_stops_process_and_catalogs_error() -> None:
    process = _Process(stderr="ERR authentication token=private-value\n")
    manager = TunnelManager(
        environment={},
        which=lambda _name: "/opt/cloudflared",
        process_factory=_ProcessFactory(process),
    )

    with pytest.raises(VoicekitError) as caught:
        await manager.open(7860, preference="cloudflared")

    assert caught.value.code == "VK-TUN-003"
    assert "private-value" not in str(caught.value)
    assert process.terminate_calls == 1


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"port": 0}, "VK-TUN-002"),
        ({"port": 7860, "preference": "url"}, "VK-TUN-002"),
        (
            {
                "port": 7860,
                "preference": "url",
                "public_url": "http://public.example",
            },
            "VK-TUN-002",
        ),
        (
            {
                "port": 7860,
                "preference": "url",
                "public_url": "https://public.example:not-a-port",
            },
            "VK-TUN-002",
        ),
        (
            {
                "port": 7860,
                "preference": "url",
                "public_url": "https://user@public.example",
            },
            "VK-TUN-002",
        ),
        ({"port": 7860, "startup_timeout_s": 0}, "VK-TUN-002"),
    ],
)
async def test_invalid_tunnel_configuration_is_cataloged(
    kwargs: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(VoicekitError) as caught:
        await TunnelManager(environment={}).open(**cast(Any, kwargs))

    assert caught.value.code == code


async def test_manual_url_handle_has_no_external_shutdown() -> None:
    handle = await TunnelManager(environment={}).open(
        7860,
        preference="url",
        public_url="https://PUBLIC.example/",
    )

    async with handle:
        assert handle.public_url == "https://public.example"

    assert handle.closed


async def test_missing_provider_requirements_are_cataloged() -> None:
    with pytest.raises(VoicekitError, match="VK-TUN-002"):
        await TunnelManager(environment={}).open(7860, preference="ngrok")
    with pytest.raises(VoicekitError, match="VK-TUN-001"):
        await TunnelManager(environment={}, which=lambda _name: None).open(
            7860,
            preference="cloudflared",
        )


def test_probe_endpoint_accepts_only_its_ephemeral_challenge() -> None:
    app = FastAPI()
    probe = TunnelProbe(token="challenge")
    probe.install(app)

    with TestClient(app) as client:
        with client.websocket_connect(probe.path) as websocket:
            websocket.send_text("challenge")
            assert websocket.receive_text() == "challenge"
        with client.websocket_connect(probe.path) as websocket:
            websocket.send_text("wrong")
            with pytest.raises(WebSocketDisconnect) as caught:
                websocket.receive_text()
            assert caught.value.code == 1008


async def test_probe_client_completes_real_local_websocket_round_trip() -> None:
    async def echo(websocket: ServerConnection) -> None:
        await websocket.send(await websocket.recv())

    async with serve(echo, "127.0.0.1", 0) as server:
        socket = server.sockets[0]
        port = cast("tuple[str, int]", socket.getsockname())[-1]
        await TunnelProbe(path="/probe", token="round-trip").verify(
            f"http://127.0.0.1:{port}",
            allow_insecure_localhost=True,
        )


async def test_probe_wrong_response_and_invalid_origin_are_cataloged() -> None:
    async def wrong(websocket: ServerConnection) -> None:
        await websocket.recv()
        await websocket.send("wrong")

    async with serve(wrong, "127.0.0.1", 0) as server:
        socket = server.sockets[0]
        port = cast("tuple[str, int]", socket.getsockname())[-1]
        with pytest.raises(VoicekitError, match="VK-TUN-004"):
            await TunnelProbe(token="expected").verify(
                f"http://127.0.0.1:{port}",
                allow_insecure_localhost=True,
            )

    with pytest.raises(VoicekitError, match="VK-TUN-002"):
        await TunnelProbe().verify("http://public.example")
    with pytest.raises(VoicekitError, match="VK-TUN-002"):
        await TunnelProbe().verify("https://public.example", timeout_s=0)


def test_probe_rejects_unsafe_path_and_hides_token_from_repr() -> None:
    probe = TunnelProbe(path="relative", token="private-challenge")
    assert "private-challenge" not in repr(probe)

    with pytest.raises(VoicekitError, match="VK-TUN-002"):
        probe.install(FastAPI())
