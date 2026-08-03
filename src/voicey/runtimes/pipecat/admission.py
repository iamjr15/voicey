"""Race-free per-instance call admission."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass

from voicey.errors import VoiceyError


@dataclass(frozen=True, slots=True)
class AdmissionLease:
    """Opaque capacity reservation released exactly once."""

    call_id: str
    token: str


class AdmissionController:
    """Reserve call slots atomically before an answer is exposed."""

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise VoiceyError(
                "VY-RUN-004",
                detail="limits.max_concurrent must reserve at least one call slot.",
            )
        self.max_concurrent = max_concurrent
        self._active: dict[str, AdmissionLease] = {}
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def acquire(self, call_id: str) -> AdmissionLease:
        """Reserve one slot or raise the stable busy error."""
        async with self._lock:
            existing = self._active.get(call_id)
            if existing is not None:
                return existing
            if len(self._active) >= self.max_concurrent:
                raise VoiceyError(
                    "VY-RUN-004",
                    detail=f"instance capacity {self.max_concurrent} is already in use.",
                )
            lease = AdmissionLease(
                call_id=call_id,
                token=secrets.token_urlsafe(32),
            )
            self._active[call_id] = lease
            return lease

    async def claim(self, call_id: str, token: str) -> AdmissionLease:
        """Authenticate a media connection against its answer-time reservation."""
        async with self._lock:
            lease = self._active.get(call_id)
            if lease is None or not secrets.compare_digest(lease.token, token):
                raise VoiceyError(
                    "VY-RUN-005",
                    detail="media connection has no matching answer-time reservation.",
                )
            return lease

    async def release(self, lease: AdmissionLease) -> bool:
        """Release only the exact active reservation."""
        async with self._lock:
            current = self._active.get(lease.call_id)
            if current != lease:
                return False
            del self._active[lease.call_id]
            return True
