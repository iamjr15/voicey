"""Authenticated WebSocket round-trip probe for public tunnel readiness."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketState
from websockets.asyncio.client import connect

from voicey.errors import VoiceyError


@dataclass(frozen=True, slots=True)
class TunnelProbe:
    """An ephemeral challenge endpoint installed before a public listener starts."""

    path: str = "/_voicey/tunnel/ws"
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)

    def install(self, app: FastAPI) -> None:
        """Install a challenge-only route; no arbitrary echo behavior is exposed."""
        if not self.path.startswith("/") or "?" in self.path or "#" in self.path:
            raise VoiceyError(
                "VY-TUN-002",
                detail="tunnel probe path must be an absolute path without query or fragment.",
            )

        @app.websocket(self.path)
        async def tunnel_probe(websocket: WebSocket) -> None:  # pyright: ignore[reportUnusedFunction]
            await websocket.accept()
            try:
                received = await asyncio.wait_for(websocket.receive_text(), timeout=5)
                if not secrets.compare_digest(received, self.token):
                    await websocket.close(code=1008, reason="VY-TUN-004")
                    return
                await websocket.send_text(self.token)
            finally:
                if websocket.application_state is not WebSocketState.DISCONNECTED:
                    await websocket.close(code=1000)

    async def verify(
        self,
        public_url: str,
        *,
        timeout_s: float = 10,
        allow_insecure_localhost: bool = False,
    ) -> None:
        """Require the exact challenge to traverse a WebSocket upgrade and return."""
        if timeout_s <= 0:
            raise VoiceyError("VY-TUN-002", detail="tunnel probe timeout must be positive.")
        websocket_url = _websocket_url(
            public_url,
            self.path,
            allow_insecure_localhost=allow_insecure_localhost,
        )
        deadline = asyncio.get_running_loop().time() + timeout_s
        last_error: Exception | None = None
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                assert last_error is not None
                raise VoiceyError(
                    "VY-TUN-004",
                    detail=f"WebSocket tunnel probe failed with {type(last_error).__name__}.",
                ) from last_error
            try:
                async with connect(
                    websocket_url,
                    open_timeout=min(remaining, 5),
                    close_timeout=min(remaining, 5),
                    ping_interval=None,
                    max_size=4096,
                    proxy=None if allow_insecure_localhost else True,
                ) as websocket:
                    await websocket.send(self.token)
                    response = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=min(remaining, 5),
                    )
                if not isinstance(response, str) or not secrets.compare_digest(
                    response,
                    self.token,
                ):
                    raise VoiceyError(
                        "VY-TUN-004",
                        detail="WebSocket tunnel probe returned the wrong challenge.",
                    )
                return
            except VoiceyError:
                raise
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(min(0.25, max(0, remaining)))


def _websocket_url(
    public_url: str,
    path: str,
    *,
    allow_insecure_localhost: bool,
) -> str:
    parsed = urlsplit(public_url.rstrip("/"))
    is_local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme == "https":
        scheme = "wss"
    elif parsed.scheme == "http" and allow_insecure_localhost and is_local:
        scheme = "ws"
    else:
        raise VoiceyError(
            "VY-TUN-002",
            detail="tunnel probe requires HTTPS; insecure HTTP is test-only on localhost.",
        )
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise VoiceyError("VY-TUN-002", detail="invalid tunnel probe public URL.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise VoiceyError("VY-TUN-002", detail="tunnel probe URL has an invalid port.") from exc
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    authority = host if port is None else f"{host}:{port}"
    encoded_path = quote(path, safe="/-._~")
    return f"{scheme}://{authority}{encoded_path}"
