"""Live provider-key validation and secure in-flow collection services."""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from voicekit.config.catalog import (
    DEFAULT_PROVIDER_CATALOG,
    ProviderCatalog,
    ProviderCatalogEntry,
    ProviderKind,
)
from voicekit.errors import VoicekitError

KeyStatus = Literal["valid", "invalid", "indeterminate", "missing"]
_TEMPLATE = re.compile(r"\$\{(?P<name>[A-Z][A-Z0-9_]*)\}")
LIVEKIT_ENV_VARS = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")


@dataclass(frozen=True, slots=True)
class KeyCheck:
    """One safe provider credential result with no secret-bearing response body."""

    provider: str
    env_names: tuple[str, ...]
    status: KeyStatus
    detail: str
    fix: str


class AsyncHttpClient(Protocol):
    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> httpx.Response: ...


class KeyValidator(Protocol):
    async def validate(
        self,
        kind: ProviderKind,
        identifier: str,
        values: Mapping[str, str],
    ) -> KeyCheck: ...


class RuntimeKeyValidator(Protocol):
    async def validate(self, values: Mapping[str, str]) -> KeyCheck: ...


class _HttpxKeyClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> httpx.Response:
        return await self._client.get(url, headers=headers, timeout=timeout_s)


class ProviderKeyValidator:
    """Validate catalog credentials against their authenticated read endpoint."""

    def __init__(
        self,
        *,
        client: AsyncHttpClient | None = None,
        catalog: ProviderCatalog = DEFAULT_PROVIDER_CATALOG,
        timeout_s: float = 8,
    ) -> None:
        if timeout_s <= 0:
            raise VoicekitError("VK-CLI-004", detail="key validation timeout must be positive.")
        self._client = client
        self._catalog = catalog
        self.timeout_s = timeout_s

    async def validate(
        self,
        kind: ProviderKind,
        identifier: str,
        values: Mapping[str, str],
    ) -> KeyCheck:
        entry = self._catalog.get(kind, identifier)
        if entry is None:
            raise VoicekitError(
                "VK-CLI-005",
                detail=f"provider {kind}/{identifier} is absent from the catalog.",
            )
        missing = tuple(name for name in entry.key_env_vars if not values.get(name))
        provider = identifier.split("/", maxsplit=1)[0]
        if missing:
            return KeyCheck(
                provider=provider,
                env_names=entry.key_env_vars,
                status="missing",
                detail=f"{', '.join(missing)} is missing.",
                fix=f"Run `voicekit keys add {provider}`.",
            )
        url, headers = _request(entry, values)
        if self._client is None:
            async with httpx.AsyncClient(follow_redirects=False) as client:
                return await self._validate_with_client(
                    _HttpxKeyClient(client),
                    provider=provider,
                    entry=entry,
                    url=url,
                    headers=headers,
                )
        return await self._validate_with_client(
            self._client,
            provider=provider,
            entry=entry,
            url=url,
            headers=headers,
        )

    async def _validate_with_client(
        self,
        client: AsyncHttpClient,
        *,
        provider: str,
        entry: ProviderCatalogEntry,
        url: str,
        headers: Mapping[str, str],
    ) -> KeyCheck:
        try:
            response = await client.get(url, headers=headers, timeout_s=self.timeout_s)
        except (httpx.HTTPError, TimeoutError):
            return KeyCheck(
                provider=provider,
                env_names=entry.key_env_vars,
                status="indeterminate",
                detail="The provider validation endpoint was unreachable.",
                fix=(
                    "Check network/provider status, then rerun validation; "
                    "do not replace the key yet."
                ),
            )
        if 200 <= response.status_code < 300:
            return KeyCheck(
                provider=provider,
                env_names=entry.key_env_vars,
                status="valid",
                detail="Authenticated provider read succeeded.",
                fix="No action required.",
            )
        if response.status_code == 401:
            return KeyCheck(
                provider=provider,
                env_names=entry.key_env_vars,
                status="invalid",
                detail="The provider rejected the credential with HTTP 401.",
                fix=f"Paste a current {provider} key with `voicekit keys add {provider}`.",
            )
        return KeyCheck(
            provider=provider,
            env_names=entry.key_env_vars,
            status="indeterminate",
            detail=f"The provider returned HTTP {response.status_code}; validity is inconclusive.",
            fix=(
                "Check account state and provider status, then retry "
                "without rotating speculatively."
            ),
        )


class LiveKitKeyValidator:
    """Validate a LiveKit project using an authenticated, read-only API call."""

    async def validate(self, values: Mapping[str, str]) -> KeyCheck:
        missing = tuple(name for name in LIVEKIT_ENV_VARS if not values.get(name))
        if missing:
            return KeyCheck(
                provider="livekit",
                env_names=LIVEKIT_ENV_VARS,
                status="missing",
                detail=f"{', '.join(missing)} is missing.",
                fix="Run `voicekit keys add livekit`.",
            )
        try:
            from livekit import api
        except ImportError:
            return KeyCheck(
                provider="livekit",
                env_names=LIVEKIT_ENV_VARS,
                status="indeterminate",
                detail="The LiveKit API package is not installed.",
                fix='Install with `uv pip install "voicekit[livekit]"`, then retry.',
            )

        client: api.LiveKitAPI | None = None
        try:
            client = api.LiveKitAPI(
                url=values["LIVEKIT_URL"],
                api_key=values["LIVEKIT_API_KEY"],
                api_secret=values["LIVEKIT_API_SECRET"],
            )
            await client.room.list_rooms(api.ListRoomsRequest())
        except ValueError:
            return KeyCheck(
                provider="livekit",
                env_names=LIVEKIT_ENV_VARS,
                status="invalid",
                detail="The LiveKit URL, API key, or API secret is malformed.",
                fix="Replace the project credentials with `voicekit keys add livekit`.",
            )
        except Exception as exc:
            status_code = getattr(exc, "status", None)
            invalid = status_code in {401, 403}
            return KeyCheck(
                provider="livekit",
                env_names=LIVEKIT_ENV_VARS,
                status="invalid" if invalid else "indeterminate",
                detail=(
                    "The LiveKit project rejected the credentials."
                    if invalid
                    else "The LiveKit project endpoint was unreachable or inconclusive."
                ),
                fix=(
                    "Replace the project credentials with `voicekit keys add livekit`."
                    if invalid
                    else "Check the project URL and network, then rerun validation."
                ),
            )
        finally:
            if client is not None:
                await client.aclose()
        return KeyCheck(
            provider="livekit",
            env_names=LIVEKIT_ENV_VARS,
            status="valid",
            detail="Authenticated LiveKit room-list read succeeded.",
            fix="No action required.",
        )


def required_entries(
    models: Mapping[str, str],
    *,
    carrier: str | None,
    catalog: ProviderCatalog = DEFAULT_PROVIDER_CATALOG,
) -> tuple[ProviderCatalogEntry, ...]:
    """Return selected provider entries once, preserving STT→LLM→TTS→carrier order."""
    found: list[ProviderCatalogEntry] = []
    seen: set[tuple[str, ...]] = set()
    for kind in ("stt", "llm", "tts"):
        identifier = models.get(kind)
        entry = None if identifier is None else catalog.get(kind, identifier)
        if entry is None:
            raise VoicekitError(
                "VK-CLI-005",
                detail=f"model selection {kind}={identifier!r} is absent from the catalog.",
            )
        key = entry.key_env_vars
        if key not in seen:
            found.append(entry)
            seen.add(key)
    if carrier is not None:
        entry = catalog.get("carrier", carrier)
        if entry is None:
            raise VoicekitError(
                "VK-CLI-005",
                detail=f"carrier {carrier!r} is absent from the catalog.",
            )
        key = entry.key_env_vars
        if key not in seen:
            found.append(entry)
    return tuple(found)


def mask_value(value: str) -> str:
    """Show presence and a tiny suffix without exposing credential material."""
    if not value:
        return "missing"
    if len(value) <= 4:
        return "••••"
    return f"••••{value[-4:]}"


def _request(
    entry: ProviderCatalogEntry,
    values: Mapping[str, str],
) -> tuple[str, dict[str, str]]:
    url = _expand(entry.validation_url, values)
    headers: dict[str, str] = {}
    for name, template in entry.validation_headers.items():
        expanded = _expand(template, values)
        if expanded.startswith("Basic "):
            credential = expanded.removeprefix("Basic ")
            expanded = "Basic " + base64.b64encode(credential.encode()).decode()
        headers[name] = expanded
    return url, headers


def _expand(template: str, values: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        try:
            return values[name]
        except KeyError as exc:
            raise VoicekitError(
                "VK-CLI-004",
                detail=f"{name} is required to validate this provider.",
            ) from exc

    return _TEMPLATE.sub(replace, template)
