"""Typed lead integrations. Replace the TODO gateway, not the conversation contract."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from voicekit import results, tool

Fit = Literal["priority", "standard", "nurture"]


class LeadGateway(Protocol):
    """Boundary for a CRM and follow-up calendar."""

    def create(
        self,
        *,
        name: str,
        email: str,
        company: str,
        need: str,
        timeline: str,
        budget_range: str,
        company_size: int,
        fit: Fit,
    ) -> str: ...

    def follow_up_slots(self, lead_reference: str, timezone: str) -> tuple[str, ...]: ...

    def schedule(self, lead_reference: str, start_iso: str, timezone: str) -> str: ...


@dataclass(frozen=True, slots=True)
class TodoLeadGateway:
    """Deterministic stub; replace with authenticated idempotent CRM calls."""

    def create(
        self,
        *,
        name: str,
        email: str,
        company: str,
        need: str,
        timeline: str,
        budget_range: str,
        company_size: int,
        fit: Fit,
    ) -> str:
        for value, field in (
            (name, "name"),
            (company, "company"),
            (need, "need"),
            (timeline, "timeline"),
            (budget_range, "budget_range"),
        ):
            _require_text(value, field)
        _validate_email(email)
        _validate_company_size(company_size)
        digest = hashlib.sha256(
            f"{email.casefold()}|{company}|{need}|{timeline}|{budget_range}|{fit}".encode()
        ).hexdigest()
        return f"LEAD-{digest[:10].upper()}"

    def follow_up_slots(self, lead_reference: str, timezone: str) -> tuple[str, ...]:
        _validate_reference(lead_reference)
        _validate_timezone(timezone)
        return (
            "2026-08-10T10:00:00",
            "2026-08-10T14:00:00",
            "2026-08-11T11:30:00",
        )

    def schedule(self, lead_reference: str, start_iso: str, timezone: str) -> str:
        _validate_reference(lead_reference)
        _validate_start(start_iso, timezone)
        digest = hashlib.sha256(f"{lead_reference}|{start_iso}|{timezone}".encode()).hexdigest()
        return f"FOLLOW-{digest[:10].upper()}"


_gateway: LeadGateway = TodoLeadGateway()


def configure_lead_gateway(gateway: LeadGateway) -> None:
    """Install the business integration during application startup."""
    global _gateway
    _gateway = gateway


@tool
def qualify_inquiry(
    need: str,
    timeline: str,
    budget_range: str,
    company_size: int,
) -> dict[str, str]:
    """Classify routing fit from business inquiry facts only."""
    for value, field in (
        (need, "need"),
        (timeline, "timeline"),
        (budget_range, "budget_range"),
    ):
        _require_text(value, field)
    _validate_company_size(company_size)
    urgent = any(token in timeline.casefold() for token in ("now", "week", "urgent"))
    fit: Fit = "priority" if urgent and company_size >= 20 else "standard"
    if "exploring" in timeline.casefold():
        fit = "nurture"
    return {"status": "qualified", "fit": fit}


@tool(say_while_running="I'm saving your inquiry.", mutating=True)
def capture_lead(
    name: str,
    email: str,
    company: str,
    need: str,
    timeline: str,
    budget_range: str,
    company_size: int,
    fit: Fit,
    consent: bool,
) -> dict[str, str]:
    """Store the inquiry only after explicit follow-up consent."""
    if not consent:
        raise ValueError("consent must be true before contact data is stored")
    reference = _gateway.create(
        name=name,
        email=email,
        company=company,
        need=need,
        timeline=timeline,
        budget_range=budget_range,
        company_size=company_size,
        fit=fit,
    )
    result = {"status": "captured", "reference": reference, "fit": fit}
    results.set("lead", result)
    results.set_outcome("lead_captured")
    return result


@tool(say_while_running="Let me find follow-up times.")
def search_follow_up_slots(lead_reference: str, timezone: str) -> dict[str, object]:
    """Return up to three follow-up slots for an already captured lead."""
    slots = _gateway.follow_up_slots(lead_reference, timezone)
    return {"status": "available" if slots else "unavailable", "slots": list(slots[:3])}


@tool(say_while_running="I'm scheduling that follow-up.", mutating=True)
def schedule_lead_followup(
    lead_reference: str,
    start_iso: str,
    timezone: str,
) -> dict[str, str]:
    """Schedule a confirmed follow-up for an already captured lead."""
    reference = _gateway.schedule(lead_reference, start_iso, timezone)
    result = {
        "status": "scheduled",
        "reference": reference,
        "lead_reference": lead_reference,
        "start_iso": start_iso,
        "timezone": timezone,
    }
    results.set("followup", result)
    results.set_outcome("lead_followup_scheduled")
    return result


def _validate_email(value: str) -> None:
    if value.count("@") != 1 or value.startswith("@") or value.endswith("@"):
        raise ValueError("email must contain a local part and domain")


def _validate_reference(value: str) -> None:
    if not value.startswith("LEAD-") or len(value) < 10:
        raise ValueError("lead_reference must be the LEAD- value from capture_lead")


def _validate_company_size(value: int) -> None:
    if not 1 <= value <= 1_000_000:
        raise ValueError("company_size must be between 1 and 1000000")


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


def _require_text(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} must not be blank")
