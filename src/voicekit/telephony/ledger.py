"""Synchronous FULL-durability ledger for carrier mutations and call intents."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeAlias, cast

from voicekit.errors import VoicekitError
from voicekit.security.files import ensure_private_directory, ensure_private_file

RouteState: TypeAlias = Literal[
    "prepared",
    "applied",
    "ambiguous",
    "restored",
    "conflict",
    "failed",
]
IntentState: TypeAlias = Literal[
    "prepared",
    "submitted",
    "ambiguous",
    "reconciled",
    "rejected",
    "terminal",
    "conflict",
]
ProvisionState: TypeAlias = Literal[
    "prepared",
    "applying",
    "applied",
    "rolling_back",
    "rolled_back",
    "ambiguous",
    "conflict",
    "failed",
]
RouteSettings: TypeAlias = dict[str, str | None]


@dataclass(frozen=True, slots=True)
class RouteRecord:
    token: str
    provider: str
    number: str
    number_sid: str
    snapshot: RouteSettings
    applied: RouteSettings
    state: RouteState
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OutboundIntent:
    intent_id: str
    provider: str
    from_number: str
    to_number: str
    target: dict[str, object]
    state: IntentState
    provider_call_id: str | None
    created_at: datetime
    updated_at: datetime
    last_status: str | None


@dataclass(frozen=True, slots=True)
class ProvisioningRecord:
    """Crash-safe multi-provider SIP provisioning operation."""

    operation_id: str
    provider: str
    number: str
    snapshot: dict[str, object]
    planned: dict[str, object]
    resources: tuple[dict[str, object], ...]
    state: ProvisionState
    created_at: datetime
    updated_at: datetime


class TelephonyLedger:
    """Crash-reopenable carrier state with serialized immediate transactions."""

    SCHEMA_VERSION = 2

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        try:
            ensure_private_directory(path.parent)
            ensure_private_file(path)
            self._connection = sqlite3.connect(
                path,
                timeout=5,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._initialize_schema()
            ensure_private_file(path)
        except VoicekitError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise VoicekitError(
                "VK-TEL-005", detail="telephony ledger initialization failed."
            ) from exc

    def close(self) -> None:
        """Checkpoint and close the ledger."""
        with self._lock:
            try:
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._connection.close()
            except sqlite3.Error as exc:
                raise VoicekitError("VK-TEL-005", detail="telephony ledger close failed.") from exc

    def prepare_route(
        self,
        *,
        provider: str,
        number: str,
        number_sid: str,
        snapshot: RouteSettings,
        applied: RouteSettings,
        now: datetime | None = None,
    ) -> RouteRecord:
        """Persist rollback data before the first external carrier mutation."""
        token = f"route_{uuid.uuid4().hex}"
        timestamp = _utc(now)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO telephony_routes(
                    token, provider, number, number_sid, snapshot_json,
                    applied_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                """,
                (
                    token,
                    provider,
                    number,
                    number_sid,
                    _json(snapshot),
                    _json(applied),
                    _iso(timestamp),
                    _iso(timestamp),
                ),
            )
        return self.get_route(token)

    def transition_route(
        self,
        token: str,
        *,
        expected: tuple[RouteState, ...],
        state: RouteState,
        now: datetime | None = None,
    ) -> RouteRecord:
        """CAS one route state so concurrent recovery cannot overwrite evidence."""
        placeholders = ",".join("?" for _ in expected)
        with self._transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE telephony_routes
                SET state = ?, updated_at = ?
                WHERE token = ? AND state IN ({placeholders})
                """,
                (state, _iso(_utc(now)), token, *expected),
            )
            if cursor.rowcount != 1:
                raise VoicekitError(
                    "VK-TEL-006",
                    detail=f"routing token {token!r} is no longer in {expected!r}.",
                )
        return self.get_route(token)

    def get_route(self, token: str) -> RouteRecord:
        row = self._fetchone(
            "SELECT * FROM telephony_routes WHERE token = ?",
            (token,),
        )
        if row is None:
            raise VoicekitError("VK-TEL-006", detail=f"unknown routing token {token!r}.")
        return _route_record(row)

    def open_routes(self, *, provider: str) -> tuple[RouteRecord, ...]:
        rows = self._fetchall(
            """
            SELECT * FROM telephony_routes
            WHERE provider = ? AND state IN ('prepared', 'applied', 'ambiguous')
            ORDER BY created_at
            """,
            (provider,),
        )
        return tuple(_route_record(row) for row in rows)

    def prepare_intent(
        self,
        *,
        intent_id: str,
        provider: str,
        from_number: str,
        to_number: str,
        target: dict[str, object],
        now: datetime | None = None,
    ) -> OutboundIntent:
        """Persist an id before invoking a non-idempotent carrier create API."""
        timestamp = _utc(now)
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO telephony_intents(
                        intent_id, provider, from_number, to_number, target_json,
                        state, provider_call_id, created_at, updated_at, last_status
                    ) VALUES (?, ?, ?, ?, ?, 'prepared', NULL, ?, ?, NULL)
                    """,
                    (
                        intent_id,
                        provider,
                        from_number,
                        to_number,
                        _json(target),
                        _iso(timestamp),
                        _iso(timestamp),
                    ),
                )
        except VoicekitError as exc:
            if isinstance(exc.__cause__, sqlite3.IntegrityError):
                raise VoicekitError(
                    "VK-TEL-007",
                    detail=f"outbound intent {intent_id!r} already exists.",
                ) from exc
            raise
        return self.get_intent(intent_id)

    def transition_intent(
        self,
        intent_id: str,
        *,
        expected: tuple[IntentState, ...],
        state: IntentState,
        provider_call_id: str | None = None,
        last_status: str | None = None,
        now: datetime | None = None,
    ) -> OutboundIntent:
        """CAS an outbound outcome while preserving its one durable identity."""
        placeholders = ",".join("?" for _ in expected)
        assignments = ["state = ?", "updated_at = ?", "last_status = ?"]
        parameters: list[object] = [state, _iso(_utc(now)), last_status]
        if provider_call_id is not None:
            assignments.append("provider_call_id = ?")
            parameters.append(provider_call_id)
        parameters.extend((intent_id, *expected))
        with self._transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE telephony_intents
                SET {", ".join(assignments)}
                WHERE intent_id = ? AND state IN ({placeholders})
                """,
                parameters,
            )
            if cursor.rowcount != 1:
                raise VoicekitError(
                    "VK-TEL-007",
                    detail=f"outbound intent {intent_id!r} changed during reconciliation.",
                )
        return self.get_intent(intent_id)

    def bind_callback(
        self,
        intent_id: str,
        *,
        provider_call_id: str,
        provider_status: str,
        terminal: bool,
        now: datetime | None = None,
    ) -> OutboundIntent:
        """Bind a callback correlation id, detecting conflicting call SIDs."""
        current = self.get_intent(intent_id)
        if current.provider_call_id not in {None, provider_call_id}:
            self.transition_intent(
                intent_id,
                expected=(current.state,),
                state="conflict",
                last_status="callback_sid_conflict",
                now=now,
            )
            raise VoicekitError(
                "VK-TEL-007",
                detail=f"outbound intent {intent_id!r} matched multiple provider calls.",
            )
        next_state: IntentState = "terminal" if terminal else "submitted"
        allowed: tuple[IntentState, ...] = (
            "prepared",
            "submitted",
            "ambiguous",
            "reconciled",
            "terminal",
        )
        return self.transition_intent(
            intent_id,
            expected=allowed,
            state=next_state,
            provider_call_id=provider_call_id,
            last_status=provider_status,
            now=now,
        )

    def get_intent(self, intent_id: str) -> OutboundIntent:
        row = self._fetchone(
            "SELECT * FROM telephony_intents WHERE intent_id = ?",
            (intent_id,),
        )
        if row is None:
            raise VoicekitError("VK-TEL-007", detail=f"unknown outbound intent {intent_id!r}.")
        return _intent_record(row)

    def unresolved_intents(self, *, provider: str) -> tuple[OutboundIntent, ...]:
        rows = self._fetchall(
            """
            SELECT * FROM telephony_intents
            WHERE provider = ? AND state IN ('prepared', 'ambiguous')
            ORDER BY created_at
            """,
            (provider,),
        )
        return tuple(_intent_record(row) for row in rows)

    def prepare_provisioning(
        self,
        *,
        provider: str,
        number: str,
        snapshot: dict[str, object],
        planned: dict[str, object],
        now: datetime | None = None,
    ) -> ProvisioningRecord:
        """Persist the complete rollback snapshot before any SIP mutation."""
        operation_id = f"provision_{uuid.uuid4().hex}"
        timestamp = _utc(now)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO telephony_provisioning(
                    operation_id, provider, number, snapshot_json, planned_json,
                    resources_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, '[]', 'prepared', ?, ?)
                """,
                (
                    operation_id,
                    provider,
                    number,
                    _json(snapshot),
                    _json(planned),
                    _iso(timestamp),
                    _iso(timestamp),
                ),
            )
        return self.get_provisioning(operation_id)

    def append_provisioned_resource(
        self,
        operation_id: str,
        *,
        resource: dict[str, object],
        expected: tuple[ProvisionState, ...] = ("prepared", "applying"),
        now: datetime | None = None,
    ) -> ProvisioningRecord:
        """Record each external resource immediately after confirmed creation."""
        current = self.get_provisioning(operation_id)
        if current.state not in expected:
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"provisioning operation {operation_id!r} changed concurrently.",
            )
        resources = [*current.resources, resource]
        placeholders = ",".join("?" for _ in expected)
        with self._transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE telephony_provisioning
                SET resources_json = ?, state = 'applying', updated_at = ?
                WHERE operation_id = ? AND state IN ({placeholders})
                """,
                (
                    _json(resources),
                    _iso(_utc(now)),
                    operation_id,
                    *expected,
                ),
            )
            if cursor.rowcount != 1:
                raise VoicekitError(
                    "VK-TEL-006",
                    detail=f"provisioning operation {operation_id!r} lost its fence.",
                )
        return self.get_provisioning(operation_id)

    def transition_provisioning(
        self,
        operation_id: str,
        *,
        expected: tuple[ProvisionState, ...],
        state: ProvisionState,
        now: datetime | None = None,
    ) -> ProvisioningRecord:
        """CAS a provisioning state for exclusive recovery/rollback ownership."""
        placeholders = ",".join("?" for _ in expected)
        with self._transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE telephony_provisioning
                SET state = ?, updated_at = ?
                WHERE operation_id = ? AND state IN ({placeholders})
                """,
                (state, _iso(_utc(now)), operation_id, *expected),
            )
            if cursor.rowcount != 1:
                raise VoicekitError(
                    "VK-TEL-006",
                    detail=f"provisioning operation {operation_id!r} changed concurrently.",
                )
        return self.get_provisioning(operation_id)

    def get_provisioning(self, operation_id: str) -> ProvisioningRecord:
        row = self._fetchone(
            "SELECT * FROM telephony_provisioning WHERE operation_id = ?",
            (operation_id,),
        )
        if row is None:
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"unknown provisioning operation {operation_id!r}.",
            )
        return _provisioning_record(row)

    def open_provisioning(self, *, provider: str) -> tuple[ProvisioningRecord, ...]:
        rows = self._fetchall(
            """
            SELECT * FROM telephony_provisioning
            WHERE provider = ?
              AND state IN ('prepared', 'applying', 'rolling_back', 'ambiguous')
            ORDER BY created_at
            """,
            (provider,),
        )
        return tuple(_provisioning_record(row) for row in rows)

    def provisioning_records(self, *, provider: str) -> tuple[ProvisioningRecord, ...]:
        """List immutable provisioning evidence, including completed rollbacks."""
        rows = self._fetchall(
            """
            SELECT * FROM telephony_provisioning
            WHERE provider = ?
            ORDER BY created_at
            """,
            (provider,),
        )
        return tuple(_provisioning_record(row) for row in rows)

    def _initialize_schema(self) -> None:
        current = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if current > self.SCHEMA_VERSION:
            raise VoicekitError(
                "VK-TEL-005",
                detail=f"telephony schema {current} is newer than supported.",
            )
        if current == self.SCHEMA_VERSION:
            return
        with self._lock:
            try:
                if current == 0:
                    self._connection.executescript(
                        """
                BEGIN IMMEDIATE;
                CREATE TABLE telephony_routes (
                    token TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    number TEXT NOT NULL,
                    number_sid TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    applied_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'prepared', 'applied', 'ambiguous',
                            'restored', 'conflict', 'failed'
                        )
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX telephony_routes_open_idx
                    ON telephony_routes(provider, state, created_at);

                CREATE TABLE telephony_intents (
                    intent_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    from_number TEXT NOT NULL,
                    to_number TEXT NOT NULL,
                    target_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'prepared', 'submitted', 'ambiguous', 'reconciled',
                            'rejected', 'terminal', 'conflict'
                        )
                    ),
                    provider_call_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_status TEXT
                );
                CREATE UNIQUE INDEX telephony_intents_provider_call_idx
                    ON telephony_intents(provider, provider_call_id)
                    WHERE provider_call_id IS NOT NULL;
                CREATE INDEX telephony_intents_unresolved_idx
                    ON telephony_intents(provider, state, created_at);
                COMMIT;
                """
                    )
                    current = 1
                if current == 1:
                    self._connection.executescript(
                        f"""
                BEGIN IMMEDIATE;
                CREATE TABLE telephony_provisioning (
                    operation_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    number TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    planned_json TEXT NOT NULL,
                    resources_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'prepared', 'applying', 'applied', 'rolling_back',
                            'rolled_back', 'ambiguous', 'conflict', 'failed'
                        )
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX telephony_provisioning_open_idx
                    ON telephony_provisioning(provider, state, created_at);
                PRAGMA user_version={self.SCHEMA_VERSION};
                COMMIT;
                """
                    )
            except sqlite3.Error as exc:
                self._connection.rollback()
                raise VoicekitError(
                    "VK-TEL-005",
                    detail="telephony schema initialization failed.",
                ) from exc

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise VoicekitError(
                    "VK-TEL-005",
                    detail="telephony transaction failed.",
                ) from exc
            try:
                yield self._connection
            except VoicekitError:
                self._connection.rollback()
                raise
            except sqlite3.Error as exc:
                self._connection.rollback()
                raise VoicekitError(
                    "VK-TEL-005",
                    detail="telephony transaction failed.",
                ) from exc
            except BaseException:
                self._connection.rollback()
                raise
            else:
                try:
                    self._connection.commit()
                except sqlite3.Error as exc:
                    raise VoicekitError(
                        "VK-TEL-005",
                        detail="telephony commit failed.",
                    ) from exc

    def _fetchone(
        self,
        sql: str,
        parameters: tuple[object, ...],
    ) -> sqlite3.Row | None:
        with self._lock:
            try:
                return self._connection.execute(sql, parameters).fetchone()
            except sqlite3.Error as exc:
                raise VoicekitError("VK-TEL-005", detail="telephony ledger read failed.") from exc

    def _fetchall(
        self,
        sql: str,
        parameters: tuple[object, ...],
    ) -> list[sqlite3.Row]:
        with self._lock:
            try:
                return list(self._connection.execute(sql, parameters).fetchall())
            except sqlite3.Error as exc:
                raise VoicekitError("VK-TEL-005", detail="telephony ledger read failed.") from exc


def _route_record(row: sqlite3.Row) -> RouteRecord:
    return RouteRecord(
        token=str(row["token"]),
        provider=str(row["provider"]),
        number=str(row["number"]),
        number_sid=str(row["number_sid"]),
        snapshot=cast("RouteSettings", json.loads(str(row["snapshot_json"]))),
        applied=cast("RouteSettings", json.loads(str(row["applied_json"]))),
        state=cast("RouteState", str(row["state"])),
        created_at=_parse(str(row["created_at"])),
        updated_at=_parse(str(row["updated_at"])),
    )


def _intent_record(row: sqlite3.Row) -> OutboundIntent:
    return OutboundIntent(
        intent_id=str(row["intent_id"]),
        provider=str(row["provider"]),
        from_number=str(row["from_number"]),
        to_number=str(row["to_number"]),
        target=cast("dict[str, object]", json.loads(str(row["target_json"]))),
        state=cast("IntentState", str(row["state"])),
        provider_call_id=(
            None if row["provider_call_id"] is None else str(row["provider_call_id"])
        ),
        created_at=_parse(str(row["created_at"])),
        updated_at=_parse(str(row["updated_at"])),
        last_status=None if row["last_status"] is None else str(row["last_status"]),
    )


def _provisioning_record(row: sqlite3.Row) -> ProvisioningRecord:
    resources = cast("list[dict[str, object]]", json.loads(str(row["resources_json"])))
    return ProvisioningRecord(
        operation_id=str(row["operation_id"]),
        provider=str(row["provider"]),
        number=str(row["number"]),
        snapshot=cast("dict[str, object]", json.loads(str(row["snapshot_json"]))),
        planned=cast("dict[str, object]", json.loads(str(row["planned_json"]))),
        resources=tuple(resources),
        state=cast("ProvisionState", str(row["state"])),
        created_at=_parse(str(row["created_at"])),
        updated_at=_parse(str(row["updated_at"])),
    )


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise VoicekitError("VK-TEL-005", detail="telephony ledger timestamp is naive.")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
