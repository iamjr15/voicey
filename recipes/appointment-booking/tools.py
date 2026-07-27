"""Typed calendar tools. Replace the TODO gateway, not the conversation contract."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from voicekit import results, tool


class CalendarGateway(Protocol):
    """Business-calendar boundary implemented by the user's real integration."""

    def available_slots(self, day: str, timezone: str) -> tuple[str, ...]: ...

    def book(
        self,
        start_iso: str,
        timezone: str,
        name: str,
        email: str,
        purpose: str,
    ) -> str: ...

    def reschedule(self, reference: str, new_start_iso: str, timezone: str) -> None: ...

    def cancel(self, reference: str) -> None: ...


@dataclass(frozen=True, slots=True)
class TodoCalendarGateway:
    """Deterministic local gateway.

    TODO: replace this class with authenticated calls to the business calendar
    API. Mutating calls should use the appointment reference as an idempotency
    key when the provider supports one.
    """

    def available_slots(self, day: str, timezone: str) -> tuple[str, ...]:
        _validate_day(day)
        _validate_timezone(timezone)
        return (
            f"{day}T09:00:00",
            f"{day}T11:30:00",
            f"{day}T15:00:00",
        )

    def book(
        self,
        start_iso: str,
        timezone: str,
        name: str,
        email: str,
        purpose: str,
    ) -> str:
        del purpose
        _validate_timezone(timezone)
        _require_text(start_iso, "start_iso")
        _require_text(name, "name")
        _validate_email(email)
        digest = hashlib.sha256(f"{start_iso}|{timezone}|{email.casefold()}".encode()).hexdigest()
        return f"APT-{digest[:10].upper()}"

    def reschedule(self, reference: str, new_start_iso: str, timezone: str) -> None:
        _validate_reference(reference)
        _require_text(new_start_iso, "new_start_iso")
        _validate_timezone(timezone)

    def cancel(self, reference: str) -> None:
        _validate_reference(reference)


_gateway: CalendarGateway = TodoCalendarGateway()


def configure_calendar_gateway(gateway: CalendarGateway) -> None:
    """Install the process-wide gateway once during application startup."""
    global _gateway
    _gateway = gateway


@tool(say_while_running="Let me check the calendar.")
def search_available_slots(day: str, timezone: str) -> dict[str, object]:
    """Return up to three open appointment start times for an ISO date and timezone."""
    slots = _gateway.available_slots(day, timezone)
    return {
        "status": "available" if slots else "unavailable",
        "day": day,
        "timezone": timezone,
        "slots": list(slots[:3]),
    }


@tool(say_while_running="I'm booking that now.")
def book_appointment(
    start_iso: str,
    timezone: str,
    name: str,
    email: str,
    purpose: str,
) -> dict[str, str]:
    """Book the caller's confirmed slot and return its stable appointment reference."""
    reference = _gateway.book(start_iso, timezone, name, email, purpose)
    result = {
        "status": "booked",
        "reference": reference,
        "start_iso": start_iso,
        "timezone": timezone,
    }
    results.set("appointment", result)
    results.set_outcome("appointment_booked")
    return result


@tool(say_while_running="I'm looking up that appointment.")
def find_appointment(reference: str, email: str) -> dict[str, str]:
    """Validate the caller's appointment reference and email before a change."""
    _validate_reference(reference)
    _validate_email(email)
    return {
        "status": "found",
        "reference": reference,
        "email": email,
    }


@tool(say_while_running="I'm moving that appointment now.")
def reschedule_appointment(
    reference: str,
    new_start_iso: str,
    timezone: str,
) -> dict[str, str]:
    """Move the confirmed appointment to the caller's newly confirmed start time."""
    _gateway.reschedule(reference, new_start_iso, timezone)
    result = {
        "status": "rescheduled",
        "reference": reference,
        "start_iso": new_start_iso,
        "timezone": timezone,
    }
    results.set("appointment", result)
    results.set_outcome("appointment_rescheduled")
    return result


@tool(say_while_running="I'm cancelling that appointment now.")
def cancel_appointment(reference: str) -> dict[str, str]:
    """Cancel the appointment only after the caller confirms the destructive change."""
    _gateway.cancel(reference)
    result = {"status": "cancelled", "reference": reference}
    results.set("appointment", result)
    results.set_outcome("appointment_cancelled")
    return result


def _validate_day(value: str) -> None:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("day must be an ISO date such as 2026-08-05") from exc


def _validate_timezone(value: str) -> None:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("timezone must be an IANA name such as America/New_York") from exc


def _validate_email(value: str) -> None:
    if value.count("@") != 1 or value.startswith("@") or value.endswith("@"):
        raise ValueError("email must contain a local part and domain")


def _validate_reference(value: str) -> None:
    if not value.startswith("APT-") or len(value) < 8:
        raise ValueError("reference must be the APT- value from a booking confirmation")


def _require_text(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} must not be blank")
