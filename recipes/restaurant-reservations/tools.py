"""Typed restaurant tools. Replace the TODO gateway, not the conversation contract."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from voicey import results, tool


class RestaurantGateway(Protocol):
    """Boundary for a real reservation platform."""

    def available_times(
        self, date_iso: str, preferred_time: str, timezone: str, party_size: int
    ) -> tuple[str, ...]: ...

    def reserve(
        self,
        *,
        start_iso: str,
        timezone: str,
        party_size: int,
        name: str,
        phone: str,
        special_requests: str,
    ) -> str: ...

    def waitlist(
        self,
        *,
        date_iso: str,
        preferred_time: str,
        timezone: str,
        party_size: int,
        name: str,
        phone: str,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class TodoRestaurantGateway:
    """Deterministic stub; replace with an authenticated, idempotent API client."""

    def available_times(
        self, date_iso: str, preferred_time: str, timezone: str, party_size: int
    ) -> tuple[str, ...]:
        _validate_request(date_iso, preferred_time, timezone, party_size)
        return (
            ()
            if party_size > 8
            else (
                f"{date_iso}T18:00:00",
                f"{date_iso}T19:30:00",
                f"{date_iso}T21:00:00",
            )
        )

    def reserve(
        self,
        *,
        start_iso: str,
        timezone: str,
        party_size: int,
        name: str,
        phone: str,
        special_requests: str,
    ) -> str:
        _validate_start(start_iso, timezone)
        _validate_party_size(party_size)
        _require_text(name, "name")
        _validate_phone(phone)
        digest = hashlib.sha256(
            f"{start_iso}|{timezone}|{party_size}|{phone}|{special_requests}".encode()
        ).hexdigest()
        return f"RES-{digest[:10].upper()}"

    def waitlist(
        self,
        *,
        date_iso: str,
        preferred_time: str,
        timezone: str,
        party_size: int,
        name: str,
        phone: str,
    ) -> str:
        _validate_request(date_iso, preferred_time, timezone, party_size)
        _require_text(name, "name")
        _validate_phone(phone)
        digest = hashlib.sha256(
            f"{date_iso}|{preferred_time}|{timezone}|{party_size}|{phone}".encode()
        ).hexdigest()
        return f"WAIT-{digest[:10].upper()}"


_gateway: RestaurantGateway = TodoRestaurantGateway()


def configure_restaurant_gateway(gateway: RestaurantGateway) -> None:
    """Install the business integration during application startup."""
    global _gateway
    _gateway = gateway


@tool(say_while_running="Let me check table availability.")
def search_tables(
    date_iso: str,
    preferred_time: str,
    timezone: str,
    party_size: int,
) -> dict[str, object]:
    """Return up to three bookable local start times."""
    times = _gateway.available_times(date_iso, preferred_time, timezone, party_size)
    return {"status": "available" if times else "unavailable", "times": list(times[:3])}


@tool(say_while_running="I'm reserving that table now.", mutating=True)
def create_reservation(
    start_iso: str,
    timezone: str,
    party_size: int,
    name: str,
    phone: str,
    special_requests: str = "",
) -> dict[str, object]:
    """Create a table reservation after the caller confirms every field."""
    reference = _gateway.reserve(
        start_iso=start_iso,
        timezone=timezone,
        party_size=party_size,
        name=name,
        phone=phone,
        special_requests=special_requests,
    )
    result: dict[str, object] = {
        "status": "reserved",
        "reference": reference,
        "start_iso": start_iso,
        "timezone": timezone,
        "party_size": party_size,
        "special_requests": special_requests,
    }
    results.set("reservation", result)
    results.set_outcome("restaurant_reserved")
    return result


@tool(say_while_running="I'm adding you to the waitlist.", mutating=True)
def join_waitlist(
    date_iso: str,
    preferred_time: str,
    timezone: str,
    party_size: int,
    name: str,
    phone: str,
) -> dict[str, object]:
    """Join the waitlist only after explicit confirmation."""
    reference = _gateway.waitlist(
        date_iso=date_iso,
        preferred_time=preferred_time,
        timezone=timezone,
        party_size=party_size,
        name=name,
        phone=phone,
    )
    result: dict[str, object] = {
        "status": "waitlisted",
        "reference": reference,
        "date_iso": date_iso,
        "preferred_time": preferred_time,
        "timezone": timezone,
        "party_size": party_size,
    }
    results.set("reservation", result)
    results.set_outcome("restaurant_waitlisted")
    return result


def _validate_request(date_iso: str, preferred_time: str, timezone: str, party_size: int) -> None:
    try:
        datetime.fromisoformat(f"{date_iso}T{preferred_time}")
    except ValueError as exc:
        raise ValueError("date_iso and preferred_time must be valid ISO values") from exc
    _validate_timezone(timezone)
    _validate_party_size(party_size)


def _validate_start(start_iso: str, timezone: str) -> None:
    try:
        datetime.fromisoformat(start_iso)
    except ValueError as exc:
        raise ValueError("start_iso must be an ISO local datetime") from exc
    _validate_timezone(timezone)


def _validate_timezone(value: str) -> None:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("timezone must be an IANA timezone") from exc


def _validate_party_size(value: int) -> None:
    if not 1 <= value <= 30:
        raise ValueError("party_size must be between 1 and 30")


def _validate_phone(value: str) -> None:
    if not value.startswith("+") or not value[1:].isdigit() or len(value) < 8:
        raise ValueError("phone must be E.164")


def _require_text(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} must not be blank")
