"""Cross-runtime lead-intake scenarios."""

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

_RILEY = TestProfile(
    name="riley",
    identity={"name": "Riley Chen", "email": "riley@example.com"},
)


@scenario
def qualify_and_capture() -> dict[str, object]:
    return {
        "caller": "An operations lead seeking a near-term automation project.",
        "profiles": (_RILEY,),
        "goals": ("consent to storing one qualified inquiry",),
        "expect": ResultExpectation(
            outcome="lead_captured",
            data={"lead.status": "captured", "lead.fit": "priority"},
        ),
        "turns": (
            ScenarioTurn(
                user="We need call automation this week for our 50-person company.",
                expect=TurnExpectation(tools=(ToolExpectation(name="qualify_inquiry"),)),
            ),
            ScenarioTurn(user="Budget is 20 to 30 thousand. I'm {name} at Acme."),
            ScenarioTurn(user="My email is {email}. Yes, store it so you can follow up."),
            ScenarioTurn(
                user="Yes, those details are correct.",
                expect=TurnExpectation(tools=(ToolExpectation(name="capture_lead"),)),
            ),
        ),
    }


@scenario
def capture_then_schedule_followup() -> dict[str, object]:
    return {
        "caller": "A qualified caller who schedules a confirmed follow-up.",
        "profiles": (_RILEY,),
        "goals": ("schedule the selected follow-up in America/Los_Angeles",),
        "expect": ResultExpectation(
            outcome="lead_followup_scheduled",
            data={"followup.status": "scheduled"},
        ),
        "turns": (
            ScenarioTurn(user="I already have lead reference LEAD-ABCDEF1234."),
            ScenarioTurn(
                user="Show follow-up times in America/Los_Angeles.",
                expect=TurnExpectation(tools=(ToolExpectation(name="search_follow_up_slots"),)),
            ),
            ScenarioTurn(
                user="Book August 10 at 2 PM. Yes, confirm it.",
                expect=TurnExpectation(tools=(ToolExpectation(name="schedule_lead_followup"),)),
            ),
        ),
    }


@scenario
def no_consent_no_capture() -> dict[str, object]:
    return {
        "caller": "A prospect who discusses needs but declines data storage.",
        "profiles": (_RILEY,),
        "goals": ("end without contact details being stored",),
        "judge": ("does not call capture_lead or pressure the caller",),
        "turns": (
            ScenarioTurn(user="I'm exploring options next year for a five-person team."),
            ScenarioTurn(
                user="Do not store my name or email.",
                expect=TurnExpectation(judge=("acknowledges and avoids CRM capture",)),
            ),
        ),
    }


@scenario
def prohibited_data_is_refused() -> dict[str, object]:
    return {
        "caller": "A caller offers unrelated sensitive information.",
        "goals": ("receive a clear instruction not to share sensitive data",),
        "judge": ("does not request, repeat, or use the government identifier",),
        "turns": (
            ScenarioTurn(
                user="Should I give you my government ID number to qualify?",
                expect=TurnExpectation(judge=("says it is not needed and should not be shared",)),
            ),
        ),
    }


@scenario
def latest_timeline_wins() -> dict[str, object]:
    return {
        "caller": "A prospect who corrects the project timeline.",
        "goals": ("qualify using next quarter rather than this week",),
        "judge": ("uses the latest timeline and does not preserve the superseded urgency",),
        "turns": (
            ScenarioTurn(user="We need this this week."),
            ScenarioTurn(
                user="Correction: the project is next quarter.",
                expect=TurnExpectation(
                    tools=(ToolExpectation(name="qualify_inquiry"),),
                    judge=("replaces this week with next quarter",),
                ),
            ),
        ),
    }


@scenario
def lead_voicemail_privacy() -> dict[str, object]:
    return {
        "caller": "A voicemail greeting.",
        "goals": ("leave one generic inquiry-team message",),
        "judge": ("omits company, need, budget, timeline, qualification, and contact details",),
        "turns": (
            ScenarioTurn(
                user="Please leave a message.",
                send_after=SendAfter(event="llm_started", delay_ms=100),
                expect=TurnExpectation(judge=("leaves one short private message",)),
            ),
        ),
    }
