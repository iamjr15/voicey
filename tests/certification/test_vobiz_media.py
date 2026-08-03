"""Installed Pipecat 1.6 Vobiz/Plivo media protocol certification."""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from typing import cast

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.serializers.plivo import PlivoFrameSerializer

from voicey.telephony.twilio import (
    decode_mulaw,
    dominant_frequency,
    rms_energy,
    tone_pcm16,
)


def _pcm16(samples: Sequence[int]) -> bytes:
    return b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples)


def _serializer() -> PlivoFrameSerializer:
    return PlivoFrameSerializer(
        stream_id="vobiz-stream",
        call_id="vobiz-call",
        params=PlivoFrameSerializer.InputParams(
            plivo_sample_rate=8000,
            sample_rate=8000,
            auto_hang_up=False,
        ),
    )


async def test_native_serializer_roundtrips_vobiz_pcmu_play_audio_and_media() -> None:
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))
    source = tone_pcm16(
        frequency_hz=1000,
        sample_rate=8000,
        duration_s=0.02,
        amplitude=12000,
    )
    encoded = await serializer.serialize(
        OutputAudioRawFrame(
            audio=_pcm16(source),
            sample_rate=8000,
            num_channels=1,
        )
    )
    message = json.loads(cast("str", encoded))
    payload = base64.b64decode(message["media"]["payload"])
    assert message["event"] == "playAudio"
    assert message["streamId"] == "vobiz-stream"
    assert message["media"]["contentType"] == "audio/x-mulaw"
    assert message["media"]["sampleRate"] == 8000
    assert len(payload) == 160
    assert rms_energy(decode_mulaw(payload)) > 7000
    assert dominant_frequency(decode_mulaw(payload), sample_rate=8000) == pytest.approx(
        1000,
        abs=10,
    )

    decoded = await serializer.deserialize(
        json.dumps(
            {
                "event": "media",
                "streamId": "vobiz-stream",
                "media": {"payload": message["media"]["payload"]},
            }
        )
    )
    assert isinstance(decoded, InputAudioRawFrame)
    assert decoded.sample_rate == 8000
    assert decoded.num_channels == 1
    assert len(decoded.audio) == 320


async def test_native_serializer_barge_in_uses_vobiz_clear_audio() -> None:
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=8000))
    cleared = await serializer.serialize(InterruptionFrame())
    assert json.loads(cast("str", cleared)) == {
        "event": "clearAudio",
        "streamId": "vobiz-stream",
    }


def test_vobiz_never_enables_plivo_hardcoded_auto_hangup() -> None:
    params = _serializer()._params  # pyright: ignore[reportPrivateUsage]
    assert not params.auto_hang_up
