"""User-owned results companion surfaces and bounded maintenance work."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from voicekit.errors import VoicekitError
from voicekit.obs.records import CallRecord, CallStatus
from voicekit.relay.auth import FenceSigner, RelayKeyring
from voicekit.relay.journal import RelayJournal
from voicekit.relay.recording import CarrierCallbackIngress, add_carrier_callback_routes
from voicekit.relay.service import RelayRepository, RepositoryRelayBackend, create_relay_app
from voicekit.results.delivery import DeliveryRun, DeliveryWorker
from voicekit.results.recovery import (
    DurableProviderObservationReconciler,
    RecoveryCoordinator,
    RecoveryRun,
)
from voicekit.results.signing import WebhookSigner
from voicekit.storage.artifacts import ArtifactStore, RetentionWorker
from voicekit.storage.models import DeliveryRecord, RecordingSnapshot, ResultSnapshot
from voicekit.storage.repository import StorageRepository


class CompanionRepository(RelayRepository, Protocol):
    """Durable reads and maintenance mutations owned by the companion."""

    async def list_calls(
        self,
        *,
        status: CallStatus | None = None,
        limit: int = 100,
    ) -> tuple[CallRecord, ...]: ...

    async def get_result_snapshot(self, call_id: str) -> ResultSnapshot: ...

    async def list_deliveries(
        self,
        *,
        undelivered_only: bool = False,
    ) -> tuple[DeliveryRecord, ...]: ...

    async def get_provider_state(self, call_id: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class CompanionSettings:
    """Non-secret public and maintenance settings for results-service mode."""

    public_base: str
    recovery_owner: str
    admin_limit: int = 100

    def __post_init__(self) -> None:
        parsed = urlsplit(self.public_base)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or self.public_base != self.public_base.rstrip("/")
        ):
            raise VoicekitError(
                "VK-DEP-003",
                detail="results-service public base must be a normalized HTTPS URL.",
            )
        if not self.recovery_owner or not 1 <= self.admin_limit <= 1000:
            raise VoicekitError(
                "VK-DEP-003",
                detail="results-service recovery owner or admin limit is invalid.",
            )


@dataclass(frozen=True, slots=True)
class MaintenanceRun:
    """One observable, bounded companion maintenance pass."""

    recovery: RecoveryRun
    delivery: DeliveryRun
    purged_artifacts: int


class CompanionMaintenance:
    """Recover stale calls, deliver their events, then finish retention work."""

    def __init__(
        self,
        repository: CompanionRepository,
        artifact_store: ArtifactStore,
        *,
        owner_id: str,
        current_result_secret: str,
        previous_result_secret: str | None = None,
        delivery: DeliveryWorker | None = None,
        recovery: RecoveryCoordinator | None = None,
    ) -> None:
        storage = cast("StorageRepository", repository)
        self._delivery = delivery or DeliveryWorker(
            storage,
            owner_id=f"{owner_id}-delivery",
            current_secret=current_result_secret,
            previous_secret=previous_result_secret,
        )
        self._owns_delivery = delivery is None
        self._recovery = recovery or RecoveryCoordinator(
            storage,
            DurableProviderObservationReconciler(repository),
            owner_id=f"{owner_id}-recovery",
        )
        self._retention = RetentionWorker(storage, artifact_store)

    async def run_once(self) -> MaintenanceRun:
        """Run each crash-safe worker once; callers own retry scheduling."""
        recovery = await self._recovery.run_once()
        delivery = await self._delivery.run_once()
        purged = await self._retention.run_once()
        return MaintenanceRun(
            recovery=recovery,
            delivery=delivery,
            purged_artifacts=purged,
        )

    async def close(self) -> None:
        if self._owns_delivery:
            await self._delivery.close()


class CompanionService:
    """Signed relay plus protected pull and artifact surfaces."""

    def __init__(
        self,
        repository: CompanionRepository,
        journal: RelayJournal,
        artifact_store: ArtifactStore,
        *,
        keyring: RelayKeyring,
        current_result_secret: str,
        previous_result_secret: str | None,
        settings: CompanionSettings,
        artifact_ready: Callable[[], Awaitable[bool]],
        callback_ingresses: tuple[CarrierCallbackIngress, ...] = (),
    ) -> None:
        WebhookSigner(current_result_secret, previous_result_secret)
        self.repository = repository
        self.journal = journal
        self.artifact_store = artifact_store
        self.settings = settings
        self._result_secrets = (
            current_result_secret,
            *(() if previous_result_secret is None else (previous_result_secret,)),
        )
        self.backend = RepositoryRelayBackend(
            repository,
            journal,
            fences=FenceSigner(keyring),
            readiness_checks=(artifact_ready,),
        )
        self.app = create_relay_app(self.backend, keyring=keyring)
        self._add_routes(self.app)
        add_carrier_callback_routes(
            self.app,
            public_base=settings.public_base,
            ingresses=callback_ingresses,
        )

    def begin_drain(self) -> None:
        """Close new relay admission while allowing already-fenced updates."""
        self.backend.begin_drain()

    def _add_routes(self, app: FastAPI) -> None:
        @app.get("/healthz")
        async def healthz() -> JSONResponse:
            return JSONResponse(
                status_code=200 if self.backend.accepting else 503,
                content={
                    "ok": self.backend.accepting,
                    "service": "voicekit-results",
                    "signed_readiness_path": "/v1/ready",
                },
                headers={"cache-control": "no-store"},
            )

        @app.get("/recordings/{recording_id}")
        async def recording_artifact(recording_id: str, request: Request) -> Response:
            self._authorize_result_bearer(request)
            snapshot = await self.repository.get_recording(recording_id)
            if snapshot.status != "ready" or snapshot.storage_key is None:
                raise VoicekitError("VK-RES-010", detail=recording_id)
            content = await self.artifact_store.read(snapshot.storage_key)
            return Response(
                content=content,
                media_type="audio/mpeg",
                headers={"cache-control": "private, no-store"},
            )

        @app.get("/v1/admin/calls")
        async def list_calls(
            request: Request,
            status: CallStatus | None = None,
            limit: int = Query(default=self.settings.admin_limit, ge=1, le=1000),
        ) -> JSONResponse:
            self._authorize_result_bearer(request)
            calls = await self.repository.list_calls(status=status, limit=limit)
            return _private_json({"calls": [call.model_dump(mode="json") for call in calls]})

        @app.get("/v1/admin/calls/{call_id}")
        async def get_call(call_id: str, request: Request) -> JSONResponse:
            self._authorize_result_bearer(request)
            call = await self.repository.get_call(call_id)
            return _private_json({"call": call.model_dump(mode="json")})

        @app.get("/v1/admin/calls/{call_id}/result")
        async def get_result(call_id: str, request: Request) -> JSONResponse:
            self._authorize_result_bearer(request)
            result = await self.repository.get_result_snapshot(call_id)
            return _private_json({"result": result.model_dump(mode="json")})

        @app.get("/v1/admin/calls/{call_id}/recording")
        async def get_recording(call_id: str, request: Request) -> JSONResponse:
            self._authorize_result_bearer(request)
            recording = await self.repository.get_recording_for_call(call_id)
            return _private_json({"recording": _recording_payload(recording)})

        @app.get("/v1/admin/deliveries")
        async def list_deliveries(
            request: Request,
            undelivered_only: bool = False,
        ) -> JSONResponse:
            self._authorize_result_bearer(request)
            deliveries = await self.repository.list_deliveries(undelivered_only=undelivered_only)
            return _private_json(
                {"deliveries": [delivery.model_dump(mode="json") for delivery in deliveries]}
            )

        _ = (
            healthz,
            recording_artifact,
            list_calls,
            get_call,
            get_result,
            get_recording,
            list_deliveries,
        )

    def _authorize_result_bearer(self, request: Request) -> None:
        supplied = request.headers.get("authorization", "")
        if not any(
            secrets.compare_digest(supplied, f"Bearer {secret}") for secret in self._result_secrets
        ):
            raise VoicekitError(
                "VK-WEB-004",
                detail="results-service bearer authorization failed.",
            )


def _private_json(content: dict[str, object]) -> JSONResponse:
    return JSONResponse(content=content, headers={"cache-control": "private, no-store"})


def _recording_payload(recording: RecordingSnapshot | None) -> object:
    return None if recording is None else recording.model_dump(mode="json")
