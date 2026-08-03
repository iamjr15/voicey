"""Explicitly opted-in prompt drafting through the selected LLM provider."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, cast

import httpx

from voicey.errors import VoiceyError

_INSTRUCTIONS = """\
Draft a concise production starting system prompt for a real-time voice agent.
Preserve the user's intent. Include a greeting policy, scope, safe failure behavior,
and a reminder to keep spoken responses brief. Return only the system prompt.
"""


class PromptDrafter(Protocol):
    async def draft(
        self,
        llm_model: str,
        description: str,
        values: Mapping[str, str],
    ) -> str: ...


class DraftHttpClient(Protocol):
    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
    ) -> httpx.Response: ...


class _HttpxDraftClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
    ) -> httpx.Response:
        return await self._client.post(url, headers=headers, json=json)


class ProviderPromptDrafter:
    """Use raw HTTPS APIs so drafting adds no provider-SDK dependency."""

    def __init__(
        self,
        *,
        timeout_s: float = 30,
        client: DraftHttpClient | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise VoiceyError("VY-CLI-004", detail="draft timeout must be positive.")
        self.timeout_s = timeout_s
        self._client = client

    async def draft(
        self,
        llm_model: str,
        description: str,
        values: Mapping[str, str],
    ) -> str:
        provider, model = llm_model.split("/", maxsplit=1)
        try:
            if self._client is None:
                async with httpx.AsyncClient(
                    follow_redirects=False,
                    timeout=self.timeout_s,
                ) as client:
                    payload = await _draft(
                        _HttpxDraftClient(client),
                        provider,
                        model,
                        description,
                        values,
                    )
            else:
                payload = await _draft(
                    self._client,
                    provider,
                    model,
                    description,
                    values,
                )
        except VoiceyError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise VoiceyError(
                "VY-CLI-004",
                detail=(
                    f"{provider} could not draft the prompt. "
                    "The setup checkpoint is safe; retry with `voicey init --resume`."
                ),
            ) from exc
        drafted = payload.strip()
        if not drafted:
            raise VoiceyError(
                "VY-CLI-004",
                detail=f"{provider} returned an empty prompt draft.",
            )
        return drafted


async def _draft(
    client: DraftHttpClient,
    provider: str,
    model: str,
    description: str,
    values: Mapping[str, str],
) -> str:
    if provider == "anthropic":
        return await _anthropic(client, model, description, values)
    if provider == "openai":
        return await _openai(client, model, description, values)
    if provider == "google":
        return await _google(client, model, description, values)
    raise VoiceyError(
        "VY-CLI-005",
        detail=f"prompt drafting is unavailable for LLM provider {provider!r}.",
    )


async def _anthropic(
    client: DraftHttpClient,
    model: str,
    description: str,
    values: Mapping[str, str],
) -> str:
    response = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": values["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": model,
            "max_tokens": 800,
            "system": _INSTRUCTIONS,
            "messages": [{"role": "user", "content": description}],
        },
    )
    _raise_for_draft_status(response, "anthropic")
    body = _json_object(response)
    content = _object_list(body.get("content"), "Anthropic content")
    texts: list[str] = []
    for raw_block in content:
        block = _object_dict(raw_block, "Anthropic content block")
        text = block.get("text")
        if block.get("type") == "text" and isinstance(text, str):
            texts.append(text)
    return "\n".join(texts)


async def _openai(
    client: DraftHttpClient,
    model: str,
    description: str,
    values: Mapping[str, str],
) -> str:
    response = await client.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {values['OPENAI_API_KEY']}"},
        json={
            "model": model,
            "instructions": _INSTRUCTIONS,
            "input": description,
            "max_output_tokens": 800,
            "store": False,
        },
    )
    _raise_for_draft_status(response, "openai")
    body = _json_object(response)
    output = _object_list(body.get("output"), "OpenAI output")
    texts: list[str] = []
    for raw_item in output:
        item = _object_dict(raw_item, "OpenAI output item")
        if item.get("type") != "message":
            continue
        content = _object_list(item.get("content"), "OpenAI message content")
        for raw_part in content:
            part = _object_dict(raw_part, "OpenAI content part")
            text = part.get("text")
            if part.get("type") == "output_text" and isinstance(text, str):
                texts.append(text)
    return "\n".join(texts)


async def _google(
    client: DraftHttpClient,
    model: str,
    description: str,
    values: Mapping[str, str],
) -> str:
    response = await client.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": values["GEMINI_API_KEY"]},
        json={
            "systemInstruction": {"parts": [{"text": _INSTRUCTIONS}]},
            "contents": [{"role": "user", "parts": [{"text": description}]}],
            "generationConfig": {"maxOutputTokens": 800},
        },
    )
    _raise_for_draft_status(response, "google")
    body = _json_object(response)
    candidates = _object_list(body.get("candidates"), "Google candidates")
    texts: list[str] = []
    for raw_candidate in candidates:
        candidate = _object_dict(raw_candidate, "Google candidate")
        content = _object_dict(candidate.get("content"), "Google candidate content")
        parts = _object_list(content.get("parts"), "Google content parts")
        for raw_part in parts:
            part = _object_dict(raw_part, "Google content part")
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts)


def _raise_for_draft_status(response: httpx.Response, provider: str) -> None:
    if 200 <= response.status_code < 300:
        return
    raise VoiceyError(
        "VY-CLI-004",
        detail=f"{provider} prompt drafting returned HTTP {response.status_code}.",
    )


def _json_object(response: httpx.Response) -> dict[str, object]:
    body = cast("object", response.json())
    if not isinstance(body, dict):
        raise TypeError("provider response is not an object")
    return cast("dict[str, object]", body)


def _object_dict(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} is absent")
    return cast("dict[str, object]", value)


def _object_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} is absent")
    return cast("list[object]", value)
