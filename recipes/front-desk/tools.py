"""Typed front-desk integrations with a deterministic local stub."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from voicey import results, tool


class FrontDeskGateway(Protocol):
    """Boundary for approved knowledge and durable message systems."""

    def answer(self, topic: str) -> str | None: ...

    def create_message(
        self, *, name: str, callback_number: str, department: str, message: str
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class TodoFrontDeskGateway:
    """Safe local corpus; replace with authenticated organization systems."""

    def answer(self, topic: str) -> str | None:
        normalized = topic.casefold()
        if "hour" in normalized:
            return "The office is open Monday through Friday, 9 AM to 5 PM local time."
        if "address" in normalized or "location" in normalized:
            return "The office address must be configured before production."
        return None

    def create_message(
        self, *, name: str, callback_number: str, department: str, message: str
    ) -> str:
        for value, field in (
            (name, "name"),
            (department, "department"),
            (message, "message"),
        ):
            _require_text(value, field)
        _validate_phone(callback_number)
        digest = hashlib.sha256(
            f"{name}|{callback_number}|{department}|{message}".encode()
        ).hexdigest()
        return f"MSG-{digest[:10].upper()}"


_gateway: FrontDeskGateway = TodoFrontDeskGateway()


def configure_front_desk_gateway(gateway: FrontDeskGateway) -> None:
    """Install the business integration during application startup."""
    global _gateway
    _gateway = gateway


@tool(say_while_running="Let me check our approved information.")
def lookup_answer(topic: str) -> dict[str, str | None]:
    """Return one approved answer or an explicit not-found result."""
    _require_text(topic, "topic")
    answer = _gateway.answer(topic)
    return {"status": "found" if answer else "not_found", "answer": answer}


@tool(say_while_running="I'm saving that message now.", mutating=True)
def take_message(
    name: str,
    callback_number: str,
    department: str,
    message: str,
) -> dict[str, str]:
    """Persist a confirmed callback message and return its stable reference."""
    normalized_department = department.strip().casefold()
    reference = _gateway.create_message(
        name=name,
        callback_number=callback_number,
        department=normalized_department,
        message=message,
    )
    result = {
        "status": "recorded",
        "reference": reference,
        "department": normalized_department,
        "callback_number": callback_number,
    }
    results.set("message", result)
    results.set_outcome("front_desk_message_taken")
    return result


def _validate_phone(value: str) -> None:
    if not value.startswith("+") or not value[1:].isdigit() or len(value) < 8:
        raise ValueError("callback_number must be E.164")


def _require_text(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} must not be blank")
