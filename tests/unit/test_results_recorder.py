import asyncio

import pytest

from voicekit import results
from voicekit.errors import VoicekitError


def test_results_fail_closed_outside_call() -> None:
    with pytest.raises(VoicekitError, match="VK-RES-005"):
        results.set("slot", "10:00")


def test_results_snapshot_is_detached() -> None:
    buffer = results.CallResultBuffer(call_id="call_test")

    with results.result_context(buffer):
        results.set("slot", "10:00")
        results.set_outcome("booked")
        snapshot = buffer.snapshot()
        results.set("slot", "11:00")

    assert snapshot == {
        "call_id": "call_test",
        "outcome": "booked",
        "data": {"slot": "10:00"},
    }
    assert buffer.data["slot"] == "11:00"


async def test_parallel_result_contexts_do_not_leak() -> None:
    async def record(call_number: int) -> results.CallResultBuffer:
        buffer = results.CallResultBuffer(call_id=f"call_{call_number}")
        with results.result_context(buffer):
            results.set("call_number", call_number)
            await asyncio.sleep(0)
            results.set_outcome(f"outcome_{call_number}")
        return buffer

    buffers = await asyncio.gather(*(record(number) for number in range(20)))

    assert [buffer.data["call_number"] for buffer in buffers] == list(range(20))
    assert [buffer.outcome for buffer in buffers] == [f"outcome_{number}" for number in range(20)]
