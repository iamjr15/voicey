"""Short-lived browser sessions, strict origins, and bounded signaling."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, cast
from urllib.parse import urlsplit

from fastapi import Request

from voicekit.errors import VoicekitError
from voicekit.results.signing import WebhookSigner

_JWT_HEADER = {"alg": "HS256", "typ": "JWT"}
_SESSION_KEY_CONTEXT = b"voicekit/web-session/v1"


@dataclass(frozen=True, slots=True)
class WebSessionIdentity:
    """Authenticated claim passed from signaling into the runtime host."""

    session_id: str
    nonce: str
    audience: str
    agent_name: str
    expires_at: int
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class IssuedWebSession:
    """Opaque browser token plus non-secret values needed by the admin UI."""

    token: str
    identity: WebSessionIdentity


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """Admin-visible in-memory link between browser and durable call."""

    session_id: str
    expires_at: int
    call_id: str | None
    pc_id: str | None
    active: bool


@dataclass(slots=True)
class _SessionState:
    identity: WebSessionIdentity
    pc_id: str | None = None
    call_id: str | None = None
    reserved: bool = False
    active: bool = False


class WindowRateLimiter:
    """Concurrency-safe fixed-window limiter with an actionable retry."""

    def __init__(
        self,
        *,
        limit: int,
        window_s: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if limit < 1 or window_s <= 0:
            raise VoicekitError("VK-WEB-003", detail="rate limit and window must be positive.")
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._events: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> None:
        now = self._clock()
        async with self._lock:
            events = self._events[key]
            cutoff = now - self.window_s
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                retry_s = max(1, int(events[0] + self.window_s - now) + 1)
                raise VoicekitError(
                    "VK-WEB-003",
                    detail=f"retry after {retry_s}s.",
                )
            events.append(now)


class SessionTokenManager:
    """Issue, authenticate, bind, and retire one-use browser session tokens."""

    def __init__(
        self,
        secret: str,
        *,
        audience: str,
        agent_name: str,
        ttl_s: int = 120,
        max_active: int = 8,
        issue_limit: int = 10,
        issue_window_s: float = 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        WebhookSigner(secret)
        if not audience or not agent_name or not 30 <= ttl_s <= 900 or max_active < 1:
            raise VoicekitError(
                "VK-WEB-001",
                detail="session audience, agent, TTL, or active limit is invalid.",
            )
        self.audience = audience
        self.agent_name = agent_name
        self.ttl_s = ttl_s
        self.max_active = max_active
        self._clock = clock
        self._key = hmac.new(
            secret.encode("utf-8"),
            _SESSION_KEY_CONTEXT,
            hashlib.sha256,
        ).digest()
        self._sessions: dict[str, _SessionState] = {}
        self._lock = asyncio.Lock()
        self._issue_limiter = WindowRateLimiter(
            limit=issue_limit,
            window_s=issue_window_s,
            clock=clock,
        )

    async def issue(self, *, client_key: str) -> IssuedWebSession:
        return await self.issue_for_call(client_key=client_key, reserve_call=None)

    async def issue_for_call(
        self,
        *,
        client_key: str,
        reserve_call: Callable[[], Awaitable[str]] | None,
    ) -> IssuedWebSession:
        """Mint only after an optional durable call reservation succeeds."""
        await self._issue_limiter.check(client_key)
        now = int(self._clock())
        async with self._lock:
            self._prune(now)
            active = sum(state.active or state.reserved for state in self._sessions.values())
            if active >= self.max_active:
                raise VoicekitError(
                    "VK-WEB-003",
                    detail=f"{self.max_active} browser sessions are already active.",
                )
            call_id = None if reserve_call is None else await reserve_call()
            if call_id is not None and not call_id:
                raise VoicekitError(
                    "VK-WEB-001",
                    detail="durable browser call reservation returned an empty id.",
                )
            identity = WebSessionIdentity(
                session_id=f"web_{secrets.token_urlsafe(18)}",
                nonce=secrets.token_urlsafe(18),
                audience=self.audience,
                agent_name=self.agent_name,
                expires_at=now + self.ttl_s,
                call_id=call_id,
            )
            payload: dict[str, object] = {
                "iss": "voicekit",
                "aud": identity.audience,
                "sub": identity.agent_name,
                "sid": identity.session_id,
                "nonce": identity.nonce,
                "iat": now,
                "exp": identity.expires_at,
            }
            if call_id is not None:
                payload["cid"] = call_id
            token = self._encode(payload)
            self._sessions[identity.session_id] = _SessionState(
                identity=identity,
                call_id=call_id,
            )
        return IssuedWebSession(token=token, identity=identity)

    async def authorize(
        self,
        token: str,
        *,
        pc_id: str | None,
    ) -> WebSessionIdentity:
        identity = self._decode(token)
        async with self._lock:
            state = self._sessions.get(identity.session_id)
            if state is None or state.identity != identity:
                raise VoicekitError("VK-WEB-001", detail="session was not issued here.")
            if state.pc_id is None:
                if pc_id is not None or state.reserved:
                    raise VoicekitError("VK-WEB-001", detail="session token was replayed.")
                state.reserved = True
            elif pc_id is None or not secrets.compare_digest(state.pc_id, pc_id):
                raise VoicekitError("VK-WEB-001", detail="session is bound to another peer.")
            return identity

    async def bind(
        self,
        identity: WebSessionIdentity,
        *,
        pc_id: str,
        call_id: str,
    ) -> None:
        async with self._lock:
            state = self._sessions.get(identity.session_id)
            if (
                state is None
                or state.identity != identity
                or not state.reserved
                or state.pc_id is not None
                or (
                    state.call_id is not None and not secrets.compare_digest(state.call_id, call_id)
                )
            ):
                raise VoicekitError("VK-WEB-001", detail="session cannot be bound.")
            state.pc_id = pc_id
            state.call_id = call_id
            state.reserved = False
            state.active = True

    async def cancel(self, identity: WebSessionIdentity) -> None:
        async with self._lock:
            state = self._sessions.get(identity.session_id)
            if state is not None and state.pc_id is None:
                del self._sessions[identity.session_id]

    async def reserved_call_id(self, identity: WebSessionIdentity) -> str:
        """Return the durable call bound before this token left the admin listener."""
        async with self._lock:
            state = self._sessions.get(identity.session_id)
            if state is None or state.identity != identity or state.call_id is None:
                raise VoicekitError(
                    "VK-WEB-001",
                    detail="browser session has no durable call reservation.",
                )
            return state.call_id

    async def release(self, pc_id: str) -> None:
        async with self._lock:
            for state in self._sessions.values():
                if state.pc_id == pc_id:
                    state.active = False
                    return

    async def snapshot(self, session_id: str) -> SessionSnapshot:
        async with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                raise VoicekitError("VK-WEB-001", detail="browser session does not exist.")
            return SessionSnapshot(
                session_id=session_id,
                expires_at=state.identity.expires_at,
                call_id=state.call_id,
                pc_id=state.pc_id,
                active=state.active,
            )

    def _encode(self, payload: Mapping[str, object]) -> str:
        header = _base64url(_canonical_json(_JWT_HEADER))
        body = _base64url(_canonical_json(payload))
        signature = _base64url(
            hmac.new(self._key, f"{header}.{body}".encode("ascii"), hashlib.sha256).digest()
        )
        return f"{header}.{body}.{signature}"

    def _decode(self, token: str) -> WebSessionIdentity:
        try:
            header_text, payload_text, signature_text = token.split(".")
            signed = f"{header_text}.{payload_text}".encode("ascii")
            supplied = _base64url_decode(signature_text)
            expected = hmac.new(self._key, signed, hashlib.sha256).digest()
            if not hmac.compare_digest(supplied, expected):
                raise ValueError("signature")
            header: object = json.loads(_base64url_decode(header_text))
            payload_value: object = json.loads(_base64url_decode(payload_text))
            if header != _JWT_HEADER or not isinstance(payload_value, dict):
                raise ValueError("shape")
            payload = cast("dict[str, Any]", payload_value)
            issued_at = payload.get("iat")
            expires_at = payload.get("exp")
            now = int(self._clock())
            if (
                payload.get("iss") != "voicekit"
                or payload.get("aud") != self.audience
                or payload.get("sub") != self.agent_name
                or not isinstance(issued_at, int)
                or not isinstance(expires_at, int)
                or issued_at > now + 30
                or expires_at <= now
            ):
                raise ValueError("claims")
            return WebSessionIdentity(
                session_id=_required_claim(payload, "sid"),
                nonce=_required_claim(payload, "nonce"),
                audience=_required_claim(payload, "aud"),
                agent_name=_required_claim(payload, "sub"),
                expires_at=expires_at,
                call_id=_optional_claim(payload, "cid"),
            )
        except (UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise VoicekitError("VK-WEB-001", detail="invalid browser session token.") from exc

    def _prune(self, now: int) -> None:
        expired = [
            session_id
            for session_id, state in self._sessions.items()
            if state.identity.expires_at <= now and not state.active
        ]
        for session_id in expired:
            del self._sessions[session_id]


@dataclass(frozen=True, slots=True)
class OriginPolicy:
    """Validate browser origin and public host with opt-in proxy trust."""

    allowed_origins: frozenset[str]
    expected_public_origin: str
    trusted_proxy_cidrs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized = _origin(self.expected_public_origin)
        if normalized != self.expected_public_origin or not self.allowed_origins:
            raise VoicekitError(
                "VK-WEB-002",
                detail="expected public origin or browser allowlist is invalid.",
            )
        for cidr in self.trusted_proxy_cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise VoicekitError(
                    "VK-WEB-002",
                    detail=f"invalid trusted proxy {cidr!r}.",
                ) from exc

    def validate(self, request: Request) -> None:
        origin = request.headers.get("origin", "")
        if origin not in self.allowed_origins:
            raise VoicekitError("VK-WEB-002", detail=f"origin {origin!r} is not allowed.")
        actual = _origin(f"{request.url.scheme}://{request.headers.get('host', '')}")
        forwarded_host = request.headers.get("x-forwarded-host")
        forwarded_proto = request.headers.get("x-forwarded-proto")
        if forwarded_host is not None or forwarded_proto is not None:
            if not self._trusted(request.client.host if request.client else ""):
                raise VoicekitError(
                    "VK-WEB-002",
                    detail="forwarded headers came from an untrusted peer.",
                )
            host = (forwarded_host or request.headers.get("host", "")).split(",", 1)[0].strip()
            proto = (forwarded_proto or request.url.scheme).split(",", 1)[0].strip()
            actual = _origin(f"{proto}://{host}")
        if not secrets.compare_digest(actual, self.expected_public_origin):
            raise VoicekitError(
                "VK-WEB-002",
                detail=f"request public origin {actual!r} does not match configured origin.",
            )

    def _trusted(self, host: str) -> bool:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(
            address in ipaddress.ip_network(cidr, strict=False) for cidr in self.trusted_proxy_cidrs
        )


class WebSessionSecurity:
    """FastAPI request adapter used by the Pipecat signaling routes."""

    def __init__(
        self,
        tokens: SessionTokenManager,
        origins: OriginPolicy,
        *,
        signal_limit: int = 120,
        signal_window_s: float = 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.tokens = tokens
        self.origins = origins
        self._signals = WindowRateLimiter(
            limit=signal_limit,
            window_s=signal_window_s,
            clock=clock,
        )

    def update_agent(self, agent_name: str, allowed_origins: frozenset[str]) -> None:
        """Refresh non-secret browser policy after a safe config reload."""
        if not secrets.compare_digest(agent_name, self.tokens.agent_name):
            raise VoicekitError(
                "VK-WEB-005",
                detail="changing agent.name requires restarting voicekit dev.",
            )
        self.origins = replace(self.origins, allowed_origins=allowed_origins)

    async def authorize(
        self,
        request: Request,
        *,
        pc_id: str | None,
    ) -> WebSessionIdentity:
        self.origins.validate(request)
        token = _bearer(request.headers.get("authorization"))
        identity = await self.tokens.authorize(token, pc_id=pc_id)
        await self._signals.check(identity.session_id)
        return identity

    async def bind(
        self,
        identity: object,
        *,
        pc_id: str,
        call_id: str,
    ) -> None:
        await self.tokens.bind(_identity(identity), pc_id=pc_id, call_id=call_id)

    async def cancel(self, identity: object) -> None:
        await self.tokens.cancel(_identity(identity))

    async def reserved_call_id(self, identity: object) -> str:
        return await self.tokens.reserved_call_id(_identity(identity))

    async def release(self, pc_id: str) -> None:
        await self.tokens.release(pc_id)


def _bearer(value: str | None) -> str:
    if value is None or not value.startswith("Bearer ") or len(value) <= 7:
        raise VoicekitError("VK-WEB-001", detail="Authorization bearer token is required.")
    return value[7:]


def _identity(value: object) -> WebSessionIdentity:
    if not isinstance(value, WebSessionIdentity):
        raise VoicekitError("VK-WEB-001", detail="runtime received an invalid session identity.")
    return value


def _required_claim(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(name)
    return value


def _optional_claim(payload: Mapping[str, Any], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(name)
    return value


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise VoicekitError("VK-WEB-002", detail=f"{value!r} is not an HTTP(S) origin.")
    return value.removesuffix("/")


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
