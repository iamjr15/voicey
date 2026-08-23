from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest

from voicey.cli.drafting import ProviderPromptDrafter
from voicey.errors import VoiceyError


class FakeDraftClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[str, str], Mapping[str, object]]] = []

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
    ) -> httpx.Response:
        self.requests.append((url, dict(headers), json))
        return self.responses.pop(0)


def _response(status: int, payload: object) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("POST", "https://provider.example.test"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "values", "response", "expected_url"),
    [
        (
            "anthropic/claude-sonnet-5",
            {"ANTHROPIC_API_KEY": "ant"},  # pragma: allowlist secret
            {"content": [{"type": "text", "text": "Anthropic draft"}]},
            "https://api.anthropic.com/v1/messages",
        ),
        (
            "openai/gpt-5",
            {"OPENAI_API_KEY": "openai"},  # pragma: allowlist secret
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "OpenAI draft"}],
                    }
                ]
            },
            "https://api.openai.com/v1/responses",
        ),
        (
            "google/gemini-3.5-flash",
            {"GEMINI_API_KEY": "gemini"},  # pragma: allowlist secret
            {"candidates": [{"content": {"parts": [{"text": "Google draft"}]}}]},
            (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-3.5-flash:generateContent"
            ),
        ),
    ],
)
async def test_provider_prompt_drafting_shapes(
    model: str,
    values: dict[str, str],
    response: object,
    expected_url: str,
) -> None:
    client = FakeDraftClient([_response(200, response)])

    result = await ProviderPromptDrafter(client=client).draft(
        model,
        "Help callers.",
        values,
    )

    assert result.endswith("draft")
    assert client.requests[0][0] == expected_url
    assert "Help callers." in str(client.requests[0][2])
    assert all(secret not in str(client.requests[0][2]) for secret in values.values())


@pytest.mark.asyncio
async def test_drafting_rejects_unknown_provider_http_error_and_empty_output() -> None:
    with pytest.raises(VoiceyError) as unknown:
        await ProviderPromptDrafter(client=FakeDraftClient([])).draft(
            "unknown/model",
            "Help callers.",
            {},
        )
    assert unknown.value.code == "VY-CLI-005"

    with pytest.raises(VoiceyError) as http_error:
        await ProviderPromptDrafter(
            client=FakeDraftClient([_response(429, {"error": "rate limited"})])
        ).draft(
            "openai/gpt-5",
            "Help callers.",
            {"OPENAI_API_KEY": "openai"},  # pragma: allowlist secret
        )
    assert http_error.value.code == "VY-CLI-004"
    assert "429" in str(http_error.value)

    with pytest.raises(VoiceyError) as empty:
        await ProviderPromptDrafter(
            client=FakeDraftClient([_response(200, {"content": []})])
        ).draft(
            "anthropic/claude-sonnet-5",
            "Help callers.",
            {"ANTHROPIC_API_KEY": "ant"},  # pragma: allowlist secret
        )
    assert empty.value.code == "VY-CLI-004"
    assert "empty" in str(empty.value)


@pytest.mark.asyncio
async def test_drafting_maps_malformed_provider_response() -> None:
    client = FakeDraftClient([_response(200, {"output": "not-a-list"})])

    with pytest.raises(VoiceyError) as caught:
        await ProviderPromptDrafter(client=client).draft(
            "openai/gpt-5",
            "Help callers.",
            {"OPENAI_API_KEY": "openai"},  # pragma: allowlist secret
        )

    assert caught.value.code == "VY-CLI-004"
    assert "checkpoint is safe" in str(caught.value)


def test_drafter_rejects_nonpositive_timeout() -> None:
    with pytest.raises(VoiceyError) as caught:
        ProviderPromptDrafter(timeout_s=0)
    assert caught.value.code == "VY-CLI-004"
