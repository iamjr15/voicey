from __future__ import annotations

from collections.abc import Iterable

import pytest
from starlette.requests import Request

from voicekit.errors import VoicekitError
from voicekit.playground.security import (
    OriginPolicy,
    SessionTokenManager,
    WebSessionSecurity,
    WindowRateLimiter,
)
from voicekit.results.signing import encode_secret


def _request(
    *,
    origin: str = "https://app.example",
    host: str = "voice.example",
    scheme: str = "https",
    token: str | None = None,
    client: str = "203.0.113.10",
    extra_headers: Iterable[tuple[str, str]] = (),
) -> Request:
    headers = [("origin", origin), ("host", host), *extra_headers]
    if token is not None:
        headers.append(("authorization", f"Bearer {token}"))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": scheme,
            "path": "/api/offer",
            "raw_path": b"/api/offer",
            "query_string": b"",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers
            ],
            "client": (client, 54321),
            "server": (host, 443),
        }
    )


@pytest.fixture
def secret() -> str:
    return encode_secret(b"s" * 32)


@pytest.mark.asyncio
async def test_session_token_is_one_use_peer_bound_and_admin_observable(
    secret: str,
) -> None:
    clock = [1_000.0]
    manager = SessionTokenManager(
        secret,
        audience="https://voice.example",
        agent_name="support",
        clock=lambda: clock[0],
    )

    async def reserve_call() -> str:
        return "call_1"

    issued = await manager.issue_for_call(
        client_key="local",
        reserve_call=reserve_call,
    )
    identity = await manager.authorize(issued.token, pc_id=None)
    assert identity.call_id == "call_1"
    assert await manager.reserved_call_id(identity) == "call_1"
    with pytest.raises(VoicekitError, match="replayed") as replay:
        await manager.authorize(issued.token, pc_id=None)
    assert replay.value.code == "VK-WEB-001"

    with pytest.raises(VoicekitError, match="cannot be bound"):
        await manager.bind(identity, pc_id="pc_1", call_id="call_wrong")
    await manager.bind(identity, pc_id="pc_1", call_id="call_1")
    assert await manager.authorize(issued.token, pc_id="pc_1") == identity
    with pytest.raises(VoicekitError, match="another peer"):
        await manager.authorize(issued.token, pc_id="pc_2")

    active = await manager.snapshot(identity.session_id)
    assert (active.call_id, active.pc_id, active.active) == ("call_1", "pc_1", True)
    await manager.release("pc_1")
    assert not (await manager.snapshot(identity.session_id)).active

    clock[0] += 121
    with pytest.raises(VoicekitError, match="invalid browser session token"):
        await manager.authorize(issued.token, pc_id="pc_1")


@pytest.mark.asyncio
async def test_session_token_tamper_cancel_capacity_and_issue_limits(secret: str) -> None:
    clock = [2_000.0]
    manager = SessionTokenManager(
        secret,
        audience="https://voice.example",
        agent_name="support",
        max_active=1,
        issue_limit=2,
        clock=lambda: clock[0],
    )
    first = await manager.issue(client_key="browser")
    header, payload, signature = first.token.split(".")
    replacement = "A" if signature[0] != "A" else "B"
    tampered = f"{header}.{payload}.{replacement}{signature[1:]}"
    with pytest.raises(VoicekitError, match="invalid browser session token"):
        await manager.authorize(tampered, pc_id=None)

    identity = await manager.authorize(first.token, pc_id=None)
    with pytest.raises(VoicekitError, match="already active"):
        await manager.issue(client_key="other-browser")
    await manager.cancel(identity)
    with pytest.raises(VoicekitError, match="does not exist"):
        await manager.snapshot(identity.session_id)
    second = await manager.issue(client_key="other-browser")
    assert second.identity.session_id != identity.session_id

    await manager.issue(client_key="browser")
    with pytest.raises(VoicekitError, match="retry after") as limited:
        await manager.issue(client_key="browser")
    assert limited.value.code == "VK-WEB-003"


@pytest.mark.asyncio
async def test_window_limiter_expires_entries_and_validates_configuration() -> None:
    clock = [100.0]
    limiter = WindowRateLimiter(limit=1, window_s=10, clock=lambda: clock[0])
    await limiter.check("client")
    with pytest.raises(VoicekitError) as limited:
        await limiter.check("client")
    assert limited.value.code == "VK-WEB-003"
    clock[0] += 11
    await limiter.check("client")

    with pytest.raises(VoicekitError) as invalid:
        WindowRateLimiter(limit=0, window_s=10)
    assert invalid.value.code == "VK-WEB-003"


def test_origin_policy_rejects_cross_origin_host_spoofing_and_untrusted_proxy() -> None:
    policy = OriginPolicy(
        allowed_origins=frozenset({"https://app.example"}),
        expected_public_origin="https://voice.example",
        trusted_proxy_cidrs=("127.0.0.0/8",),
    )
    policy.validate(_request())

    with pytest.raises(VoicekitError, match="not allowed"):
        policy.validate(_request(origin="https://attacker.example"))
    with pytest.raises(VoicekitError, match="does not match"):
        policy.validate(_request(host="other.example"))
    with pytest.raises(VoicekitError, match="untrusted peer"):
        policy.validate(
            _request(
                extra_headers=(
                    ("x-forwarded-host", "voice.example"),
                    ("x-forwarded-proto", "https"),
                )
            )
        )

    policy.validate(
        _request(
            host="internal:8000",
            scheme="http",
            client="127.0.0.1",
            extra_headers=(
                ("x-forwarded-host", "voice.example"),
                ("x-forwarded-proto", "https"),
            ),
        )
    )


@pytest.mark.asyncio
async def test_web_security_requires_bearer_and_applies_signal_limit(secret: str) -> None:
    manager = SessionTokenManager(
        secret,
        audience="https://voice.example",
        agent_name="support",
    )
    security = WebSessionSecurity(
        manager,
        OriginPolicy(
            allowed_origins=frozenset({"https://app.example"}),
            expected_public_origin="https://voice.example",
        ),
        signal_limit=1,
    )
    with pytest.raises(VoicekitError, match="bearer") as missing:
        await security.authorize(_request(), pc_id=None)
    assert missing.value.code == "VK-WEB-001"

    issued = await manager.issue(client_key="browser")
    request = _request(token=issued.token)
    identity = await security.authorize(request, pc_id=None)
    await security.bind(identity, pc_id="pc_1", call_id="call_1")
    with pytest.raises(VoicekitError) as limited:
        await security.authorize(request, pc_id="pc_1")
    assert limited.value.code == "VK-WEB-003"

    security.update_agent("support", frozenset({"https://reloaded.example"}))
    reloaded = _request(origin="https://reloaded.example", token=issued.token)
    with pytest.raises(VoicekitError, match="retry after"):
        await security.authorize(reloaded, pc_id="pc_1")
    with pytest.raises(VoicekitError, match="restarting voicekit dev"):
        security.update_agent("renamed", frozenset({"https://reloaded.example"}))
    await security.release("pc_1")
