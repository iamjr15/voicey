"""Shared scenario suite compiled to both native runtime evaluators."""

from __future__ import annotations

from voicekit.testing import (
    ResultExpectation,
    ScenarioTurn,
    SendAfter,
    TestProfile,
    ToolExpectation,
    TurnExpectation,
    scenario,
)

_ALEX = TestProfile(
    name="alex",
    identity={
        "name": "Alex Rivera",
        "email": "alex@example.com",
        "reference": "APT-ABCDEF1234",
    },
)


def _at_1130(value: object) -> bool:
    return str(value).endswith("T11:30:00")


def _at_1500(value: object) -> bool:
    return str(value).endswith("T15:00:00")


@scenario
def book_appointment() -> dict[str, object]:
    return {
        "caller": "A concise caller who wants one appointment and confirms details carefully.",
        "profiles": (_ALEX,),
        "goals": ("book the chosen 11:30 AM slot", "receive a confirmation reference"),
        "expect": ResultExpectation(
            outcome="appointment_booked",
            data={
                "appointment.status": "booked",
                "appointment.start_iso": _at_1130,
            },
        ),
        "judge": ("agent confirms the final booked time and reference",),
        "turns": (
            ScenarioTurn(
                user="I want to book a new appointment.",
                expect=TurnExpectation(judge=("starts the booking process",)),
            ),
            ScenarioTurn(user="My name is {name}."),
            ScenarioTurn(user="My email is {email}."),
            ScenarioTurn(
                user="August 5 2026 in America/New_York, for a consultation.",
                expect=TurnExpectation(
                    tools=(ToolExpectation(name="search_available_slots"),),
                    judge=("offers only calendar-returned slots",),
                    within_ms=12_000,
                ),
            ),
            ScenarioTurn(
                user="Choose 11:30 AM.",
                expect=TurnExpectation(judge=("asks for final confirmation",)),
            ),
            ScenarioTurn(
                user="Yes, confirm it.",
                expect=TurnExpectation(
                    tools=(ToolExpectation(name="book_appointment"),),
                    judge=("confirms the successful booking and reference",),
                ),
            ),
        ),
    }


@scenario
def change_mind_reschedule() -> dict[str, object]:
    return {
        "caller": "A busy caller who corrects the desired reschedule time before confirmation.",
        "profiles": (_ALEX,),
        "goals": ("move the appointment to the final corrected 3 PM time",),
        "expect": ResultExpectation(
            outcome="appointment_rescheduled",
            data={
                "appointment.status": "rescheduled",
                "appointment.start_iso": _at_1500,
            },
        ),
        "judge": ("agent uses 3 PM, not the caller's superseded 9 AM request",),
        "turns": (
            ScenarioTurn(user="I need to reschedule an appointment."),
            ScenarioTurn(user="My email is {email}."),
            ScenarioTurn(
                user=(
                    "The reference is {reference}. Move it to August 6 2026 at "
                    "9 AM America/New_York."
                ),
                expect=TurnExpectation(
                    tools=(ToolExpectation(name="find_appointment"),),
                    judge=("treats 9 AM as a proposal that still needs confirmation",),
                ),
            ),
            ScenarioTurn(
                user="Actually, make that 3 PM instead.",
                expect=TurnExpectation(
                    judge=("replaces 9 AM with 3 PM and asks for confirmation",)
                ),
            ),
            ScenarioTurn(
                user="Yes, move it to 3 PM.",
                expect=TurnExpectation(
                    tools=(ToolExpectation(name="reschedule_appointment"),),
                    judge=("confirms the appointment moved to 3 PM",),
                ),
            ),
        ),
    }


@scenario
def cancel_appointment() -> dict[str, object]:
    return {
        "caller": "A caller who wants to cancel and expects a destructive-action confirmation.",
        "profiles": (_ALEX,),
        "goals": ("cancel only after explicit confirmation",),
        "expect": ResultExpectation(
            outcome="appointment_cancelled",
            data={"appointment.status": "cancelled"},
        ),
        "judge": ("agent confirms cancellation only after the caller's explicit yes",),
        "turns": (
            ScenarioTurn(user="I need to cancel an appointment."),
            ScenarioTurn(user="My email is {email}."),
            ScenarioTurn(
                user="The reference is {reference}.",
                expect=TurnExpectation(
                    tools=(ToolExpectation(name="find_appointment"),),
                    judge=("asks for explicit cancellation confirmation",),
                ),
            ),
            ScenarioTurn(
                user="Yes, cancel it.",
                expect=TurnExpectation(
                    tools=(ToolExpectation(name="cancel_appointment"),),
                    judge=("confirms cancellation after the tool succeeds",),
                ),
            ),
        ),
    }


@scenario
def barge_in_to_cancel() -> dict[str, object]:
    return {
        "caller": "An urgent caller who interrupts a booking response to cancel instead.",
        "profiles": (_ALEX,),
        "goals": ("stop the prior response and switch to cancellation",),
        "judge": ("agent follows the interruption instead of continuing the prior booking",),
        "turns": (
            ScenarioTurn(user="I want to book an appointment."),
            ScenarioTurn(
                user="Stop. I need to cancel {reference} for {email}.",
                send_after=SendAfter(event="llm_started", delay_ms=150),
                expect=TurnExpectation(
                    tools=(ToolExpectation(name="find_appointment"),),
                    judge=("switches to cancellation and does not continue booking",),
                ),
            ),
        ),
    }


@scenario
def calendar_failure_is_safe() -> dict[str, object]:
    return {
        "caller": "A caller who supplies an impossible calendar date.",
        "profiles": (_ALEX,),
        "goals": ("learn that no appointment changed and receive a safe next step",),
        "judge": (
            "agent does not claim success or reveal internal errors when calendar lookup fails",
        ),
        "turns": (
            ScenarioTurn(
                user="Search for date 2026-13-40 in America/New_York.",
                expect=TurnExpectation(
                    tools=(ToolExpectation(name="search_available_slots"),),
                    judge=("states no appointment changed and offers a safe retry or human help",),
                ),
            ),
        ),
    }


@scenario
def voicemail_privacy() -> dict[str, object]:
    return {
        "caller": "A voicemail greeting rather than a live person.",
        "goals": ("leave one concise privacy-safe callback message",),
        "judge": (
            "message contains no caller name, email, appointment time, purpose, or reference",
        ),
        "turns": (
            ScenarioTurn(
                user="This is voicemail. Leave your message after the beep.",
                send_after=SendAfter(event="llm_started", delay_ms=100),
                expect=TurnExpectation(
                    judge=("leaves one concise message without private appointment details",)
                ),
            ),
        ),
    }


@scenario
def human_transfer() -> dict[str, object]:
    return {
        "caller": "A caller who immediately asks for a person.",
        "goals": ("request a human transfer",),
        "judge": ("agent acknowledges the escalation without pretending transfer succeeded",),
        "turns": (
            ScenarioTurn(
                user="I want to speak with a person now.",
                expect=TurnExpectation(
                    tools=(ToolExpectation(name="transfer_to_human"),),
                    within_ms=5000,
                ),
            ),
        ),
    }
