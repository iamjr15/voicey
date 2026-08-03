"""Shared managed-companion credential preparation and continuity checks."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from voicey.cli.environment import EnvFileStore
from voicey.errors import VoiceyError
from voicey.relay.auth import RelayCredential
from voicey.results.signing import WebhookSigner, encode_secret

_PROVIDER_SECRETS: dict[str, tuple[str, ...]] = {
    "twilio": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
    "telnyx": (
        "TELNYX_API_KEY",
        "TELNYX_PUBLIC_KEY",
        "TELNYX_CONNECTION_ID",
    ),
    "vobiz": ("VOBIZ_AUTH_ID", "VOBIZ_AUTH_TOKEN"),
    "plivo": ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"),
}


class SecretContinuityState(Protocol):
    """Non-secret state needed to reject accidental credential replacement."""

    @property
    def relay_fingerprint(self) -> str | None: ...

    @property
    def results_fingerprint(self) -> str | None: ...


@dataclass(frozen=True, slots=True, repr=False)
class ManagedSecretBundle:
    """Validated current/previous relay and results-service credentials."""

    relay_current: str
    relay_previous: str | None
    results_current: str
    results_previous: str | None
    carrier: Mapping[str, str]

    @property
    def relay(self) -> RelayCredential:
        return RelayCredential.parse(self.relay_current)

    def platform_values(self) -> dict[str, str]:
        values = {
            "VOICEY_RELAY_CREDENTIAL": self.relay_current,
            "VOICEY_RESULTS_SECRET": self.results_current,
            **self.carrier,
        }
        if self.relay_previous is not None:
            values["VOICEY_RELAY_PREVIOUS_CREDENTIAL"] = self.relay_previous
        if self.results_previous is not None:
            values["VOICEY_RESULTS_PREVIOUS_SECRET"] = self.results_previous
        return values


def prepare_managed_secrets(
    project_root: Path,
    environment: Mapping[str, str],
    callback_providers: tuple[str, ...],
    *,
    rotate: bool,
    expected_relay_fingerprint: str | None,
    expected_results_fingerprint: str | None,
) -> ManagedSecretBundle:
    """Load/generate owner-only credentials without placing values in CLI arguments."""
    store = EnvFileStore(project_root / ".env")
    persisted = store.read()
    combined = persisted | dict(environment)
    carrier: dict[str, str] = {}
    for provider in callback_providers:
        for name in _PROVIDER_SECRETS[provider]:
            value = combined.get(name, "").strip()
            if not value:
                raise VoiceyError(
                    "VY-DEP-003",
                    detail=f"{provider} callback ingestion requires {name}.",
                )
            carrier[name] = value

    relay_current = combined.get("VOICEY_RELAY_CREDENTIAL", "").strip()
    results_current = combined.get("VOICEY_RESULTS_SECRET", "").strip()
    relay_previous = combined.get("VOICEY_RELAY_PREVIOUS_CREDENTIAL", "").strip() or None
    results_previous = combined.get("VOICEY_RESULTS_PREVIOUS_SECRET", "").strip() or None
    if expected_relay_fingerprint is not None and (
        not relay_current or fingerprint(relay_current) != expected_relay_fingerprint
    ):
        raise VoiceyError(
            "VY-DEP-007",
            detail="local relay credential differs from the ledger; use the recorded secret.",
        )
    if expected_results_fingerprint is not None and (
        not results_current or fingerprint(results_current) != expected_results_fingerprint
    ):
        raise VoiceyError(
            "VY-DEP-007",
            detail="local results secret differs from the ledger; use the recorded secret.",
        )

    updates: dict[str, str] = {}
    if rotate:
        if not relay_current or not results_current:
            raise VoiceyError(
                "VY-DEP-003",
                detail="credential rotation requires existing relay and results secrets.",
            )
        RelayCredential.parse(relay_current)
        WebhookSigner(results_current)
        relay_previous = relay_current
        results_previous = results_current
        relay_current = RelayCredential.issue(f"k-{secrets.token_hex(6)}").reveal()
        results_current = encode_secret(secrets.token_bytes(32))
        updates.update(
            {
                "VOICEY_RELAY_CREDENTIAL": relay_current,
                "VOICEY_RELAY_PREVIOUS_CREDENTIAL": relay_previous,
                "VOICEY_RESULTS_SECRET": results_current,
                "VOICEY_RESULTS_PREVIOUS_SECRET": results_previous,
            }
        )
    else:
        if not relay_current:
            relay_current = RelayCredential.issue(f"k-{secrets.token_hex(6)}").reveal()
            updates["VOICEY_RELAY_CREDENTIAL"] = relay_current
        if not results_current:
            results_current = encode_secret(secrets.token_bytes(32))
            updates["VOICEY_RESULTS_SECRET"] = results_current

    RelayCredential.parse(relay_current)
    if relay_previous is not None:
        RelayCredential.parse(relay_previous)
    WebhookSigner(results_current, results_previous)
    if updates:
        store.update(updates)
    return ManagedSecretBundle(
        relay_current=relay_current,
        relay_previous=relay_previous,
        results_current=results_current,
        results_previous=results_previous,
        carrier=carrier,
    )


def validate_secret_continuity(
    state: SecretContinuityState,
    bundle: ManagedSecretBundle,
    *,
    rotate: bool,
) -> None:
    """Require an explicit rotation path for any ledgered credential change."""
    if rotate:
        if state.relay_fingerprint not in {
            None,
            fingerprint(bundle.relay_previous or ""),
        } or state.results_fingerprint not in {
            None,
            fingerprint(bundle.results_previous or ""),
        }:
            raise VoiceyError(
                "VY-DEP-007",
                detail="credential rotation source differs from the resource ledger.",
            )
        return
    if state.relay_fingerprint not in {None, fingerprint(bundle.relay_current)}:
        raise VoiceyError(
            "VY-DEP-007",
            detail="local relay credential differs from the ledger; use explicit rotation.",
        )
    if state.results_fingerprint not in {None, fingerprint(bundle.results_current)}:
        raise VoiceyError(
            "VY-DEP-007",
            detail="local results secret differs from the ledger; use explicit rotation.",
        )


def fingerprint(value: str) -> str:
    """Return a non-reversible short identity used only for continuity checks."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


__all__ = [
    "ManagedSecretBundle",
    "fingerprint",
    "prepare_managed_secrets",
    "validate_secret_continuity",
]
