"""Cross-runtime restaurant reservation scenarios."""

from __future__ import annotations

from voicey.testing import (
    ResultExpectation,
    ScenarioTurn,
    SendAfter,
    TestProfile,
    ToolExpectation,
    TurnExpectation,
    scenario,
)

_CASEY = TestProfile(
    name="casey",
    identity={"name": "Casey Morgan", "phone": "+14155550142"},
)


@scenario
def reserve_available_table() -> dict[str, object]:
    return {
        "caller": "A careful diner making one dinner reservation.",
        "profiles": (_CASEY,),
        "goals": ("reserve the confirmed 7:30 PM table for four",),
        "expect": ResultExpectation(
            outcome="restaurant_reserved",
            data={"reservation.status": "reserved", "reservation.party_size": 4},
        ),
        "judge": ("states the reservation is confirmed and gives its reference",),
        "turns": (
            ScenarioTurn(
                user="I need a dinner reservation for four.",
                expect=TurnExpectation(judge=("asks for the missing reservation date and time",)),
            ),
            ScenarioTurn(
                user=(
                    "August 8 2026 around 7 PM, America/New_York. I'm {name}, "
                    "phone {phone}, with no special requests."
                ),
                expect=TurnExpectation(tools=(ToolExpectation(name="search_tables"),)),
            ),
            ScenarioTurn(
                user="7:30 works.",
                expect=TurnExpectation(
                    judge=("restates the complete reservation and asks for confirmation",)
                ),
            ),
            ScenarioTurn(
                user="Yes, confirm all of that.",
                expect=TurnExpectation(tools=(ToolExpectation(name="create_reservation"),)),
            ),
        ),
    }


@scenario
def unavailable_party_joins_waitlist() -> dict[str, object]:
    return {
        "caller": "A large party that accepts waitlist uncertainty.",
        "profiles": (_CASEY,),
        "goals": ("join the waitlist only after learning no table is guaranteed",),
        "expect": ResultExpectation(
            outcome="restaurant_waitlisted",
            data={"reservation.status": "waitlisted", "reservation.party_size": 10},
        ),
        "judge": ("does not describe the waitlist as a confirmed table",),
        "turns": (
            ScenarioTurn(
                user=(
                    "Table for ten on August 8 2026 at 7 PM America/New_York. "
                    "I'm {name}, phone {phone}, with no special requests."
                ),
                expect=TurnExpectation(tools=(ToolExpectation(name="search_tables"),)),
            ),
            ScenarioTurn(
                user="Add me to the waitlist.",
                expect=TurnExpectation(
                    judge=("asks for confirmation before joining the waitlist",)
                ),
                runtimes=frozenset({"pipecat"}),
            ),
            ScenarioTurn(
                user="I want to join the waitlist; connect me to the waitlist specialist.",
                expect=TurnExpectation(handoff="WaitlistAgent"),
                runtimes=frozenset({"livekit"}),
            ),
            ScenarioTurn(
                user="I confirm those details and understand the waitlist is not guaranteed.",
                expect=TurnExpectation(tools=(ToolExpectation(name="join_waitlist"),)),
            ),
        ),
    }


@scenario
def latest_party_size_wins() -> dict[str, object]:
    return {
        "caller": "A diner who corrects party size before confirmation.",
        "profiles": (_CASEY,),
        "goals": ("reserve for five, not the superseded party of four",),
        "judge": ("uses the latest party size and reconfirms it",),
        "turns": (
            ScenarioTurn(
                user="Reserve for four tomorrow evening.",
                expect=TurnExpectation(judge=("asks for the missing reservation details",)),
            ),
            ScenarioTurn(
                user="Actually there will be five of us.",
                expect=TurnExpectation(
                    judge=("acknowledges five supersedes four before searching",)
                ),
            ),
        ),
    }


@scenario
def allergy_is_not_guaranteed() -> dict[str, object]:
    return {
        "caller": "A diner asking for an allergy accommodation.",
        "goals": ("record the request without receiving a safety guarantee",),
        "judge": (
            "treats the allergy note as a request and offers human help for policy questions",
        ),
        "turns": (
            ScenarioTurn(
                user="Can you guarantee a completely peanut-free kitchen?",
                expect=TurnExpectation(judge=("does not guarantee allergen safety",)),
            ),
        ),
    }


@scenario
def restaurant_voicemail_privacy() -> dict[str, object]:
    return {
        "caller": "A restaurant voicemail greeting.",
        "goals": ("leave one privacy-safe callback message",),
        "turns": (
            ScenarioTurn(
                user="You've reached voicemail. Leave a message.",
                send_after=SendAfter(event="llm_started", delay_ms=100),
                expect=TurnExpectation(
                    judge=(
                        "leaves one short generic restaurant callback message without "
                        "reservation details",
                    )
                ),
            ),
        ),
    }
