"""Installed Pipecat 1.6 Telnyx media codec and control certification."""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from typing import cast

import pytest
from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InputDTMFFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.serializers.telnyx import TelnyxFrameSerializer

from voicey.telephony.twilio import (
    FramePacer,
    JitterBuffer,
    MediaFrame,
    decode_mulaw,
    dominant_frequency,
    linear16_to_mulaw,
    rms_energy,
    tone_pcm16,
)


def _pcm16(samples: Sequence[int]) -> bytes:
    return b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples)


def _serializer() -> TelnyxFrameSerializer:
    return TelnyxFrameSerializer(
        stream_id="stream-certified",
        outbound_encoding="PCMU",
        inbound_encoding="PCMU",
        params=TelnyxFrameSerializer.InputParams(
            telnyx_sample_rate=8000,
            sample_rate=8000,
            inbound_encoding="PCMU",
            outbound_encoding="PCMU",
            auto_hang_up=False,
        ),
    )


async def test_native_serializer_roundtrips_20ms_pcmu_audio() -> None:
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))
    source = tone_pcm16(
        frequency_hz=1000,
        sample_rate=8000,
        duration_s=0.02,
        amplitude=12000,
    )
    encoded_message = await serializer.serialize(
        OutputAudioRawFrame(
            audio=_pcm16(source),
            sample_rate=8000,
            num_channels=1,
        )
    )
    message = json.loads(cast("str", encoded_message))
    payload = base64.b64decode(message["media"]["payload"])

    assert message["event"] == "media"
    assert len(payload) == 160
    assert rms_energy(decode_mulaw(payload)) > 7000
    assert dominant_frequency(decode_mulaw(payload), sample_rate=8000) == pytest.approx(
        1000,
        abs=10,
    )

    decoded = await serializer.deserialize(cast("str", encoded_message))
    assert isinstance(decoded, InputAudioRawFrame)
    assert decoded.sample_rate == 8000
    assert decoded.num_channels == 1
    assert len(decoded.audio) == 320


async def test_native_serializer_emits_clear_and_receives_dtmf() -> None:
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=16000))

    cleared = await serializer.serialize(InterruptionFrame())
    dtmf = await serializer.deserialize(json.dumps({"event": "dtmf", "dtmf": {"digit": "#"}}))
    invalid = await serializer.deserialize(
        json.dumps({"event": "dtmf", "dtmf": {"digit": "invalid"}})
    )

    assert json.loads(cast("str", cleared)) == {"event": "clear"}
    assert isinstance(dtmf, InputDTMFFrame)
    assert dtmf.button == KeypadEntry.POUND
    assert invalid is None


async def test_telnyx_raw_rtp_frames_use_20ms_pacing_and_bounded_jitter() -> None:
    now = 10.0
    sleeps: list[float] = []

    def clock() -> float:
        return now

    async def sleep(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay

    pacer = FramePacer(clock=clock, sleep=sleep)
    assert await pacer.wait() == 0
    assert await pacer.wait() == pytest.approx(0.02)
    assert sleeps == [pytest.approx(0.02)]

    payload = bytes(linear16_to_mulaw(0) for _ in range(160))
    jitter = JitterBuffer(max_late_frames=2)
    assert [item.sequence for item in jitter.push(MediaFrame(1, 0, payload))] == [1]
    assert jitter.push(MediaFrame(3, 40, payload)) == ()
    assert [item.sequence for item in jitter.push(MediaFrame(2, 20, payload))] == [2, 3]


def test_auto_hangup_requires_both_installed_serializer_credentials() -> None:
    with pytest.raises(ValueError, match="call_control_id, api_key"):
        TelnyxFrameSerializer(
            stream_id="stream-certified",
            outbound_encoding="PCMU",
            inbound_encoding="PCMU",
        )
