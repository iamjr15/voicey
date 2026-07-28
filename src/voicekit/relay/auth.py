"""Credential rotation, request signing, and opaque fencing tokens."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from voicekit.errors import VoicekitError
from voicekit.relay.models import FenceClaims
from voicekit.storage.models import CallLease

_KEY_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
_AUTH_SCHEME = "VoicekitRelay"


@dataclass(frozen=True, slots=True, repr=False)
class RelayCredential:
    """One rotatable HMAC credential; repr never exposes key material."""

    key_id: str
    secret: bytes

    def __post_init__(self) -> None:
        if not _KEY_ID.fullmatch(self.key_id) or len(self.secret) < 32:
            raise VoicekitError(
                "VK-REL-001",
                detail="relay key id or secret strength is invalid.",
            )

    @classmethod
    def issue(cls, key_id: str) -> RelayCredential:
        """Generate a new 256-bit credential."""
        return cls(key_id=key_id, secret=secrets.token_bytes(32))

    @classmethod
    def parse(cls, value: str) -> RelayCredential:
        """Parse the printable vkr_<id>_<base64url> rotation format."""
        prefix, separator, remainder = value.partition("_")
        key_id, separator_two, encoded = remainder.partition("_")
        if prefix != "vkr" or not separator or not separator_two:
            raise VoicekitError("VK-REL-001", detail="relay credential format is invalid.")
        try:
            secret = _decode(encoded)
        except (ValueError, binascii.Error) as exc:
            raise VoicekitError(
                "VK-REL-001",
                detail="relay credential encoding is invalid.",
            ) from exc
        return cls(key_id=key_id, secret=secret)

    def reveal(self) -> str:
        """Serialize only for protected secret-file or platform secret sync."""
        return f"vkr_{self.key_id}_{_encode(self.secret)}"


@dataclass(frozen=True, slots=True)
class RelayKeyring:
    """Current credential plus an optional previous rotation credential."""

    current: RelayCredential
    previous: RelayCredential | None = None

    def __post_init__(self) -> None:
        if self.previous is not None and self.previous.key_id == self.current.key_id:
            raise VoicekitError(
                "VK-REL-001",
                detail="current and previous relay key ids must differ.",
            )

    def find(self, key_id: str) -> RelayCredential | None:
        if hmac.compare_digest(self.current.key_id, key_id):
            return self.current
        if self.previous is not None and hmac.compare_digest(self.previous.key_id, key_id):
            return self.previous
        return None


class RelayRequestSigner:
    """Sign canonical method/path/body bytes with a fresh nonce."""

    def __init__(
        self,
        credential: RelayCredential,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credential = credential
        self._clock = clock

    def headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(self._clock()))
        nonce = secrets.token_urlsafe(24)
        signature = _request_signature(
            self._credential.secret,
            timestamp=timestamp,
            nonce=nonce,
            method=method,
            path=path,
            body=body,
        )
        return {
            "authorization": f"{_AUTH_SCHEME} {self._credential.key_id}",
            "x-voicekit-relay-timestamp": timestamp,
            "x-voicekit-relay-nonce": nonce,
            "x-voicekit-relay-signature": signature,
            "content-type": "application/json",
        }


class RelayRequestVerifier:
    """Verify rotation keys and replay window before a request reaches storage."""

    def __init__(
        self,
        keyring: RelayKeyring,
        *,
        tolerance: timedelta = timedelta(minutes=5),
        clock: Callable[[], float] = time.time,
    ) -> None:
        if tolerance <= timedelta(0) or tolerance > timedelta(hours=1):
            raise VoicekitError("VK-REL-001", detail="relay replay tolerance is invalid.")
        self._keyring = keyring
        self._tolerance = tolerance
        self._clock = clock

    def verify(
        self,
        headers: Mapping[str, str],
        *,
        method: str,
        path: str,
        body: bytes,
    ) -> tuple[str, str, datetime]:
        authorization = headers.get("authorization", "")
        scheme, separator, key_id = authorization.partition(" ")
        timestamp = headers.get("x-voicekit-relay-timestamp", "")
        nonce = headers.get("x-voicekit-relay-nonce", "")
        supplied = headers.get("x-voicekit-relay-signature", "")
        credential = self._keyring.find(key_id) if scheme == _AUTH_SCHEME and separator else None
        try:
            seconds = int(timestamp)
        except ValueError as exc:
            raise VoicekitError("VK-REL-003", detail="relay timestamp is malformed.") from exc
        now = self._clock()
        if (
            credential is None
            or len(nonce) < 20
            or abs(now - seconds) > self._tolerance.total_seconds()
        ):
            raise VoicekitError("VK-REL-003", detail="relay authorization is invalid or expired.")
        expected = _request_signature(
            credential.secret,
            timestamp=timestamp,
            nonce=nonce,
            method=method,
            path=path,
            body=body,
        )
        if not hmac.compare_digest(supplied, expected):
            raise VoicekitError("VK-REL-003", detail="relay request signature does not match.")
        expires_at = datetime.fromtimestamp(
            seconds + self._tolerance.total_seconds(),
            tz=UTC,
        )
        return key_id, nonce, expires_at


class FenceSigner:
    """Issue and validate server-owned generation tokens across key rotation."""

    def __init__(
        self,
        keyring: RelayKeyring,
        *,
        token_ttl: timedelta = timedelta(hours=1),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if token_ttl < timedelta(minutes=5) or token_ttl > timedelta(hours=24):
            raise VoicekitError("VK-REL-001", detail="relay fence TTL is invalid.")
        self._keyring = keyring
        self._token_ttl = token_ttl
        self._clock = clock or (lambda: datetime.now(UTC))

    def issue(self, lease: CallLease) -> str:
        claims = FenceClaims(
            call_id=lease.call_id,
            owner_id=lease.owner_id,
            generation=lease.generation,
            lease_expires_at=lease.expires_at,
            token_expires_at=self._clock() + self._token_ttl,
        )
        payload = claims.model_dump_json().encode()
        signature = hmac.digest(self._keyring.current.secret, payload, "sha256")
        return f"{self._keyring.current.key_id}.{_encode(payload)}.{_encode(signature)}"

    def verify(self, token: str, *, call_id: str) -> CallLease:
        key_id, separator, remainder = token.partition(".")
        payload_encoded, separator_two, signature_encoded = remainder.partition(".")
        credential = self._keyring.find(key_id)
        if credential is None or not separator or not separator_two:
            raise VoicekitError("VK-REL-004", detail="fence token format is invalid.")
        try:
            payload = _decode(payload_encoded)
            supplied = _decode(signature_encoded)
            expected = hmac.digest(credential.secret, payload, "sha256")
            claims = FenceClaims.model_validate_json(payload)
        except (ValueError, binascii.Error, ValidationError) as exc:
            raise VoicekitError("VK-REL-004", detail="fence token payload is invalid.") from exc
        if (
            not hmac.compare_digest(supplied, expected)
            or not hmac.compare_digest(claims.call_id, call_id)
            or self._clock() > claims.token_expires_at
        ):
            raise VoicekitError("VK-REL-004", detail="fence token is invalid or expired.")
        return CallLease(
            call_id=claims.call_id,
            owner_id=claims.owner_id,
            generation=claims.generation,
            expires_at=claims.lease_expires_at,
        )


def _request_signature(
    secret: bytes,
    *,
    timestamp: str,
    nonce: str,
    method: str,
    path: str,
    body: bytes,
) -> str:
    digest = hashlib.sha256(body).hexdigest()
    canonical = "\n".join((timestamp, nonce, method.upper(), path, digest)).encode()
    return _encode(hmac.digest(secret, canonical, "sha256"))


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
