"""Standard Webhooks signing with current+previous secret rotation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from collections.abc import Mapping
from dataclasses import dataclass

from voicey.errors import VoiceyError

SECRET_PREFIX = "whsec_"  # pragma: allowlist secret
SIGNATURE_VERSION = "v1"
DEFAULT_TOLERANCE_SECONDS = 300


@dataclass(frozen=True, slots=True)
class SignedWebhook:
    """Headers and immutable raw body for one delivery attempt."""

    headers: Mapping[str, str]
    body: bytes


def encode_secret(key: bytes) -> str:
    """Serialize raw HMAC key bytes in the Standard Webhooks secret format."""
    if not key:
        raise VoiceyError("VY-RES-001", detail="The decoded key is empty.")
    return f"{SECRET_PREFIX}{base64.b64encode(key).decode('ascii')}"


class WebhookSigner:
    """Sign and verify raw webhook bodies using up to two active secrets."""

    def __init__(self, current_secret: str, previous_secret: str | None = None) -> None:
        self._keys = (_decode_secret(current_secret),)
        if previous_secret is not None:
            self._keys += (_decode_secret(previous_secret),)

    def sign(
        self,
        event_id: str,
        body: bytes,
        *,
        timestamp: int | None = None,
    ) -> SignedWebhook:
        """Create Standard Webhooks headers for an immutable raw body."""
        if not event_id:
            raise VoiceyError("VY-RES-002", detail="webhook-id is empty.")
        issued_at = int(time.time()) if timestamp is None else timestamp
        signatures = " ".join(_signature_for(key, event_id, issued_at, body) for key in self._keys)
        return SignedWebhook(
            headers={
                "webhook-id": event_id,
                "webhook-timestamp": str(issued_at),
                "webhook-signature": signatures,
            },
            body=body,
        )

    def verify(
        self,
        headers: Mapping[str, str],
        body: bytes,
        *,
        now: int | None = None,
        tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    ) -> None:
        """Verify required headers, replay window, and any active signature."""
        normalized = {key.lower(): value for key, value in headers.items()}
        try:
            event_id = normalized["webhook-id"]
            timestamp_text = normalized["webhook-timestamp"]
            signature_text = normalized["webhook-signature"]
            timestamp = int(timestamp_text)
        except (KeyError, TypeError, ValueError) as exc:
            raise VoiceyError("VY-RES-002") from exc
        if not event_id or not signature_text:
            raise VoiceyError("VY-RES-002")

        current_time = int(time.time()) if now is None else now
        if abs(current_time - timestamp) > tolerance_seconds:
            raise VoiceyError("VY-RES-003")

        supplied = signature_text.split()
        expected = {_signature_for(key, event_id, timestamp, body) for key in self._keys}
        if not any(
            hmac.compare_digest(candidate, valid) for candidate in supplied for valid in expected
        ):
            raise VoiceyError("VY-RES-004")


def verify_webhook(
    headers: Mapping[str, str],
    body: bytes,
    *,
    current_secret: str,
    previous_secret: str | None = None,
    now: int | None = None,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> None:
    """Verify a received voicey webhook against current/previous secrets."""
    WebhookSigner(current_secret, previous_secret).verify(
        headers,
        body,
        now=now,
        tolerance_seconds=tolerance_seconds,
    )


def _decode_secret(secret: str) -> bytes:
    if not secret.startswith(SECRET_PREFIX):
        raise VoiceyError("VY-RES-001", detail="Missing whsec_ prefix.")
    encoded = secret.removeprefix(SECRET_PREFIX)
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VoiceyError("VY-RES-001", detail="The key is not valid base64.") from exc
    if not key:
        raise VoiceyError("VY-RES-001", detail="The decoded key is empty.")
    return key


def _signature_for(key: bytes, event_id: str, timestamp: int, body: bytes) -> str:
    signed_content = f"{event_id}.{timestamp}.".encode() + body
    digest = hmac.new(key, signed_content, hashlib.sha256).digest()
    encoded = base64.b64encode(digest).decode("ascii")
    return f"{SIGNATURE_VERSION},{encoded}"
