"""LiveKit lifecycle specialization over the shared fenced call machinery."""

from __future__ import annotations

from datetime import timedelta

from voicey.runtimes.pipecat.admission import AdmissionController
from voicey.runtimes.pipecat.lifecycle import (
    PipecatCall,
    PipecatCallLifecycle,
    PipecatLifecycleManager,
    PipecatRepository,
)

LiveKitCall = PipecatCall
LiveKitCallLifecycle = PipecatCallLifecycle
LiveKitRepository = PipecatRepository


class LiveKitLifecycleManager(PipecatLifecycleManager):
    """Stamp LiveKit ownership/runtime while retaining identical fencing semantics."""

    def __init__(
        self,
        repository: LiveKitRepository,
        admission: AdmissionController,
        *,
        owner_id: str | None = None,
        lease_ttl: timedelta = timedelta(seconds=30),
    ) -> None:
        super().__init__(
            repository,
            admission,
            owner_id=owner_id,
            lease_ttl=lease_ttl,
            runtime="livekit",
        )
