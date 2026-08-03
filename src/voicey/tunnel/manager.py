"""Deterministic tunnel selection and lifecycle supervision."""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from types import ModuleType
from typing import Literal, Protocol, Self, cast
from urllib.parse import urlsplit

from voicey.errors import VoiceyError

TunnelProvider = Literal["ngrok", "cloudflared", "url"]
TunnelPreference = Literal["auto", "ngrok", "cloudflared", "url"]
CloudflaredProtocol = Literal["auto", "http2"]

_TRYCLOUDFLARE_URL = re.compile(
    r"(?P<url>https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com)\b",
    re.IGNORECASE,
)
_SENSITIVE_DIAGNOSTIC = re.compile(r"(?i)\b(token|secret|password|authorization)(\s*[:=]\s*)\S+")
_MAX_DIAGNOSTIC_LINES = 20


class NgrokListener(Protocol):
    """Installed ngrok listener methods used by voicey."""

    def url(self) -> str: ...

    def close(self) -> Awaitable[None]: ...


class TunnelProcess(Protocol):
    """Subset of `asyncio.subprocess.Process` required for supervision."""

    @property
    def stdout(self) -> asyncio.StreamReader | None: ...

    @property
    def stderr(self) -> asyncio.StreamReader | None: ...

    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


ProcessFactory = Callable[..., Awaitable[asyncio.subprocess.Process]]


@dataclass(slots=True)
class TunnelHandle:
    """One public origin and an idempotent async shutdown hook."""

    provider: TunnelProvider
    public_url: str
    local_url: str
    _closer: Callable[[], Awaitable[None]] = field(repr=False)
    _closed: bool = False
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def websocket_base(self) -> str:
        """Return the public origin using the WebSocket TLS scheme."""
        return "wss://" + self.public_url.removeprefix("https://")

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        """Close exactly once, cataloging provider shutdown failures."""
        async with self._close_lock:
            if self._closed:
                return
            try:
                await self._closer()
            except VoiceyError:
                raise
            except Exception as exc:
                raise VoiceyError(
                    "VY-TUN-005",
                    detail=f"{self.provider} tunnel shutdown failed.",
                ) from exc
            self._closed = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: object,
    ) -> None:
        await self.close()


class TunnelManager:
    """Resolve ngrok-with-token before a cloudflared quick tunnel."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] = shutil.which,
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
        ngrok_module: ModuleType | None = None,
    ) -> None:
        self._environment = environment if environment is not None else os.environ
        self._which = which
        self._process_factory = process_factory
        self._ngrok_module = ngrok_module

    async def open(
        self,
        port: int,
        *,
        preference: TunnelPreference = "auto",
        public_url: str | None = None,
        cloudflared_protocol: CloudflaredProtocol = "auto",
        startup_timeout_s: float = 20,
        shutdown_timeout_s: float = 10,
    ) -> TunnelHandle:
        """Start or adopt one tunnel without shell interpolation."""
        _validate_open_options(
            port=port,
            preference=preference,
            public_url=public_url,
            cloudflared_protocol=cloudflared_protocol,
            startup_timeout_s=startup_timeout_s,
            shutdown_timeout_s=shutdown_timeout_s,
        )
        local_url = f"http://127.0.0.1:{port}"
        provider = self.resolve(preference)
        if provider == "url":
            assert public_url is not None
            return TunnelHandle(
                provider="url",
                public_url=_validate_public_url(public_url),
                local_url=local_url,
                _closer=_noop_close,
            )
        if provider == "ngrok":
            return await self._open_ngrok(
                local_url,
                startup_timeout_s=startup_timeout_s,
            )
        return await self._open_cloudflared(
            local_url,
            protocol=cloudflared_protocol,
            startup_timeout_s=startup_timeout_s,
            shutdown_timeout_s=shutdown_timeout_s,
        )

    def resolve(self, preference: TunnelPreference = "auto") -> TunnelProvider:
        """Resolve the locked provider order without starting external state."""
        if preference == "url":
            return "url"
        if preference == "ngrok":
            return "ngrok"
        if preference == "cloudflared":
            return "cloudflared"
        if preference != "auto":
            raise VoiceyError("VY-TUN-002", detail=f"unsupported tunnel {preference!r}.")
        if self._environment.get("NGROK_AUTHTOKEN", ""):
            return "ngrok"
        return "cloudflared"

    async def _open_ngrok(
        self,
        local_url: str,
        *,
        startup_timeout_s: float,
    ) -> TunnelHandle:
        token = self._environment.get("NGROK_AUTHTOKEN", "")
        if not token:
            raise VoiceyError(
                "VY-TUN-002",
                detail="NGROK_AUTHTOKEN is required when ngrok is selected.",
            )
        module = self._ngrok_module or _import_ngrok()
        forward = getattr(module, "forward", None)
        if not callable(forward):
            raise VoiceyError(
                "VY-TUN-001",
                detail="installed ngrok package has no forward() API.",
            )
        typed_listener: NgrokListener | None = None
        try:
            listener = await asyncio.wait_for(
                asyncio.to_thread(forward, local_url, authtoken=token),
                timeout=startup_timeout_s,
            )
            typed_listener = cast(NgrokListener, listener)
            public_url = _validate_public_url(typed_listener.url())
        except TimeoutError as exc:
            kill = getattr(module, "kill", None)
            if callable(kill):
                with suppress(Exception):
                    await asyncio.to_thread(kill)
            raise VoiceyError(
                "VY-TUN-003",
                detail="ngrok did not publish an endpoint before the startup deadline.",
            ) from exc
        except VoiceyError:
            if typed_listener is not None:
                with suppress(Exception):
                    await typed_listener.close()
            raise
        except Exception as exc:
            if typed_listener is not None:
                with suppress(Exception):
                    await typed_listener.close()
            raise VoiceyError(
                "VY-TUN-003",
                detail=f"ngrok startup failed with {type(exc).__name__}.",
            ) from exc
        return TunnelHandle(
            provider="ngrok",
            public_url=public_url,
            local_url=local_url,
            _closer=typed_listener.close,
        )

    async def _open_cloudflared(
        self,
        local_url: str,
        *,
        protocol: CloudflaredProtocol,
        startup_timeout_s: float,
        shutdown_timeout_s: float,
    ) -> TunnelHandle:
        executable = self._which("cloudflared")
        if executable is None:
            raise VoiceyError(
                "VY-TUN-001",
                detail="cloudflared is not installed or is absent from PATH.",
            )
        arguments = [
            executable,
            "tunnel",
            "--no-autoupdate",
            "--url",
            local_url,
        ]
        if protocol == "http2":
            arguments.extend(["--protocol", "http2"])
        try:
            process = await self._process_factory(
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise VoiceyError(
                "VY-TUN-003",
                detail=f"cloudflared could not start: {type(exc).__name__}.",
            ) from exc
        try:
            public_url, log_tasks = await _read_cloudflared_url(
                process,
                timeout_s=startup_timeout_s,
            )
        except Exception:
            await _stop_process(process, timeout_s=shutdown_timeout_s)
            raise
        return TunnelHandle(
            provider="cloudflared",
            public_url=public_url,
            local_url=local_url,
            _closer=lambda: _close_cloudflared(
                process,
                log_tasks=log_tasks,
                timeout_s=shutdown_timeout_s,
            ),
        )


async def _read_cloudflared_url(
    process: TunnelProcess,
    *,
    timeout_s: float,
) -> tuple[str, tuple[asyncio.Task[None], ...]]:
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    readers = [
        asyncio.create_task(_pump_lines(stream, queue), name=f"voicey-tunnel-log-{index}")
        for index, stream in enumerate((process.stdout, process.stderr))
        if stream is not None
    ]
    if not readers:
        raise VoiceyError(
            "VY-TUN-003",
            detail="cloudflared started without readable stdout or stderr.",
        )
    diagnostics: list[str] = []
    closed_streams = 0
    retain_readers = False
    try:
        async with asyncio.timeout(timeout_s):
            while closed_streams < len(readers):
                line = await queue.get()
                if line is None:
                    closed_streams += 1
                    continue
                match = _TRYCLOUDFLARE_URL.search(line)
                if match is not None:
                    drainer = asyncio.create_task(
                        _discard_lines(
                            queue,
                            remaining_streams=len(readers) - closed_streams,
                        ),
                        name="voicey-tunnel-log-drain",
                    )
                    retain_readers = True
                    return (
                        _validate_public_url(match.group("url")),
                        (*readers, drainer),
                    )
                if len(diagnostics) < _MAX_DIAGNOSTIC_LINES:
                    diagnostics.append(_safe_diagnostic(line))
    except TimeoutError as exc:
        raise VoiceyError(
            "VY-TUN-003",
            detail="cloudflared did not publish a quick-tunnel URL before the deadline.",
        ) from exc
    finally:
        if not retain_readers:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
    exit_detail = (
        f"exit {process.returncode}"
        if process.returncode is not None
        else "closed both log streams"
    )
    suffix = f"; last log: {diagnostics[-1]}" if diagnostics else ""
    raise VoiceyError(
        "VY-TUN-003",
        detail=f"cloudflared {exit_detail} before publishing a URL{suffix}.",
    )


async def _pump_lines(
    stream: asyncio.StreamReader,
    queue: asyncio.Queue[str | None],
) -> None:
    try:
        while line := await stream.readline():
            await queue.put(line.decode("utf-8", errors="replace").strip())
    finally:
        await queue.put(None)


async def _discard_lines(
    queue: asyncio.Queue[str | None],
    *,
    remaining_streams: int,
) -> None:
    while remaining_streams:
        if await queue.get() is None:
            remaining_streams -= 1


async def _close_cloudflared(
    process: TunnelProcess,
    *,
    log_tasks: tuple[asyncio.Task[None], ...],
    timeout_s: float,
) -> None:
    try:
        await _stop_process(process, timeout_s=timeout_s)
    finally:
        for task in log_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*log_tasks, return_exceptions=True)


async def _stop_process(process: TunnelProcess, *, timeout_s: float) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=timeout_s)
    except ProcessLookupError:
        return
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        try:
            await process.wait()
        except Exception as exc:
            raise VoiceyError(
                "VY-TUN-005",
                detail="cloudflared did not exit after a forced kill.",
            ) from exc
    except Exception as exc:
        raise VoiceyError(
            "VY-TUN-005",
            detail="cloudflared did not exit after terminate.",
        ) from exc


def _validate_open_options(
    *,
    port: int,
    preference: TunnelPreference,
    public_url: str | None,
    cloudflared_protocol: CloudflaredProtocol,
    startup_timeout_s: float,
    shutdown_timeout_s: float,
) -> None:
    if not 1 <= port <= 65535:
        raise VoiceyError("VY-TUN-002", detail="local tunnel port must be 1 through 65535.")
    if preference not in {"auto", "ngrok", "cloudflared", "url"}:
        raise VoiceyError("VY-TUN-002", detail=f"unsupported tunnel {preference!r}.")
    if preference == "url" and public_url is None:
        raise VoiceyError("VY-TUN-002", detail="--tunnel url requires an HTTPS public URL.")
    if cloudflared_protocol not in {"auto", "http2"}:
        raise VoiceyError(
            "VY-TUN-002",
            detail=f"unsupported cloudflared protocol {cloudflared_protocol!r}.",
        )
    if startup_timeout_s <= 0 or shutdown_timeout_s <= 0:
        raise VoiceyError("VY-TUN-002", detail="tunnel timeouts must be positive.")


def _validate_public_url(value: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    try:
        port = parsed.port
    except ValueError as exc:
        raise VoiceyError("VY-TUN-002", detail="tunnel public URL has an invalid port.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise VoiceyError(
            "VY-TUN-002",
            detail="tunnel public URL must be an HTTPS origin with no credentials or path.",
        )
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}"


def _safe_diagnostic(line: str) -> str:
    """Keep only a bounded, control-character-free provider diagnostic."""
    printable = "".join(character for character in line if character.isprintable())
    return _SENSITIVE_DIAGNOSTIC.sub(r"\1\2[redacted]", printable)[:240]


def _import_ngrok() -> ModuleType:
    try:
        return importlib.import_module("ngrok")
    except ImportError as exc:
        raise VoiceyError(
            "VY-TUN-001",
            detail='install the ngrok SDK with `uv pip install "voicey[tunnel]"`.',
        ) from exc


async def _noop_close() -> None:
    return None
