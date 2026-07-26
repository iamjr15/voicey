import base64

import pytest

from voicekit.errors import VoicekitError
from voicekit.results.signing import WebhookSigner, encode_secret

CURRENT_KEY = b"current-signing-key-for-tests"
PREVIOUS_KEY = b"previous-signing-key-for-tests"
BODY = b'{"event":"call.completed","id":"evt_test"}'


def test_sign_and_verify_with_rotation() -> None:
    signer = WebhookSigner(encode_secret(CURRENT_KEY), encode_secret(PREVIOUS_KEY))

    signed = signer.sign("evt_test", BODY, timestamp=1_750_000_000)

    assert signed.body == BODY
    assert signed.headers["webhook-id"] == "evt_test"
    assert signed.headers["webhook-signature"].count("v1,") == 2
    signer.verify(signed.headers, BODY, now=1_750_000_001)


def test_previous_secret_verifier_accepts_rotation_signature() -> None:
    rotating = WebhookSigner(encode_secret(CURRENT_KEY), encode_secret(PREVIOUS_KEY))
    previous_only = WebhookSigner(encode_secret(PREVIOUS_KEY))
    signed = rotating.sign("evt_test", BODY, timestamp=1_750_000_000)
    previous_signature = signed.headers["webhook-signature"].split()[1]
    headers = {**signed.headers, "webhook-signature": previous_signature}

    previous_only.verify(headers, BODY, now=1_750_000_001)


@pytest.mark.parametrize(
    ("headers_change", "body", "code"),
    [
        ({"webhook-id": ""}, BODY, "VK-RES-002"),
        ({}, b"tampered", "VK-RES-004"),
    ],
)
def test_verification_rejects_malformed_or_tampered_delivery(
    headers_change: dict[str, str],
    body: bytes,
    code: str,
) -> None:
    signer = WebhookSigner(encode_secret(CURRENT_KEY))
    signed = signer.sign("evt_test", BODY, timestamp=1_750_000_000)

    with pytest.raises(VoicekitError, match=code):
        signer.verify(
            {**signed.headers, **headers_change},
            body,
            now=1_750_000_001,
        )


def test_verification_rejects_replay_window() -> None:
    signer = WebhookSigner(encode_secret(CURRENT_KEY))
    signed = signer.sign("evt_test", BODY, timestamp=1_750_000_000)

    with pytest.raises(VoicekitError, match="VK-RES-003"):
        signer.verify(signed.headers, BODY, now=1_750_000_301)


@pytest.mark.parametrize(
    "secret",
    [
        "",
        "not-prefixed",
        "whsec_not valid base64",
        "whsec_",
    ],
)
def test_invalid_secret_fails_with_catalog_error(secret: str) -> None:
    with pytest.raises(VoicekitError, match="VK-RES-001"):
        WebhookSigner(secret)


def test_secret_serialization_is_decode_before_hmac() -> None:
    serialized = encode_secret(CURRENT_KEY)

    assert serialized.startswith("whsec_")
    assert base64.b64decode(serialized.removeprefix("whsec_")) == CURRENT_KEY
