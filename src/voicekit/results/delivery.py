"""Signed durable outbox delivery with the canonical Standard Webhooks curve."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from voicekit.results.signing import WebhookSigner
from voicekit.storage.repository import StorageRepository


@dataclass(frozen=True, slots=True)
class DeliveryRun:
    """Outcome counts for one bounded worker pass."""

    claimed: int
    delivered: int
    failed: int
    dead_lettered: int


class DeliveryWorker:
    """Lease, freshly sign, and attempt durable result deliveries."""

    def __init__(
        self,
        repository: StorageRepository,
        *,
        owner_id: str,
        current_secret: str,
        previous_secret: str | None = None,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
        jitter: Callable[[float], float] | None = None,
        lease_ttl: timedelta = timedelta(seconds=30),
    ) -> None:
        self._repository = repository
        self._owner_id = owner_id
        self._signer = WebhookSigner(current_secret, previous_secret)
        self._client = client or httpx.AsyncClient(timeout=10)
        self._owns_client = client is None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._jitter = jitter or _random_jitter
        self._lease_ttl = lease_ttl

    async def close(self) -> None:
        """Close the internally-created HTTP client."""
        if self._owns_client:
            await self._client.aclose()

    async def run_once(self, *, limit: int = 20) -> DeliveryRun:
        """Attempt each due delivery once and persist every outcome."""
        current = self._clock()
        claims = await self._repository.claim_deliveries(
            owner_id=self._owner_id,
            limit=limit,
            lease_ttl=self._lease_ttl,
            now=current,
        )
        delivered = 0
        failed = 0
        dead_lettered = 0
        for claim in claims:
            attempt_time = self._clock()
            signed = self._signer.sign(
                claim.event_id,
                claim.body,
                timestamp=int(attempt_time.timestamp()),
            )
            try:
                response = await self._client.post(
                    claim.endpoint,
                    content=signed.body,
                    headers={
                        **signed.headers,
                        "content-type": "application/json",
                        "user-agent": "voicekit-webhooks/1",
                    },
                )
                if response.is_success:
                    await self._repository.acknowledge_delivery(
                        claim,
                        now=attempt_time,
                    )
                    delivered += 1
                    continue
                error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                error = type(exc).__name__

            record = await self._repository.fail_delivery(
                claim,
                error=error,
                jitter=self._jitter,
                now=attempt_time,
            )
            failed += 1
            if record.status == "dead_lettered":
                dead_lettered += 1
        return DeliveryRun(
            claimed=len(claims),
            delivered=delivered,
            failed=failed,
            dead_lettered=dead_lettered,
        )


def _random_jitter(delay: float) -> float:
    return delay * random.uniform(0.8, 1.2)
