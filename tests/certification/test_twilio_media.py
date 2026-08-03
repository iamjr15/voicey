import base64
import json

import pytest

from voicey.errors import VoiceyError
from voicey.telephony.twilio import (
    FramePacer,
    JitterBuffer,
    MediaEvent,
    MediaFrame,
    PlaybackTracker,
    clear_message,
    decode_mulaw,
    dominant_frequency,
    linear16_to_mulaw,
    mark_message,
    media_message,
    mulaw_to_linear16,
    resample_linear,
    rms_energy,
    tone_pcm16,
)
from voicey.telephony.twilio.media import parse_stream_message

STREAM_SID = "MZ" + "1" * 32
CALL_SID = "CA" + "2" * 32


def test_mulaw_roundtrip_and_8k_to_16k_tone_loopback() -> None:
    source = tone_pcm16(
        frequency_hz=440,
        sample_rate=16000,
        duration_s=1,
        amplitude=12000,
    )
    at_8k = resample_linear(source, source_rate=16000, target_rate=8000)
    encoded = bytes(linear16_to_mulaw(sample) for sample in at_8k)
    decoded_8k = decode_mulaw(encoded)
    decoded_16k = resample_linear(decoded_8k, source_rate=8000, target_rate=16000)

    assert len(encoded) == 8000
    assert len(decoded_16k) == 16000
    assert rms_energy(decoded_16k) > 7000
    assert dominant_frequency(decoded_16k, sample_rate=16000) == pytest.approx(
        440,
        abs=2,
    )
    assert linear16_to_mulaw(0) == 0xFF
    assert mulaw_to_linear16(0xFF) == 0


def test_media_message_is_base64_mulaw_at_8k() -> None:
    pcm = tone_pcm16(
        frequency_hz=1000,
        sample_rate=16000,
        duration_s=0.02,
    )

    message = json.loads(media_message(STREAM_SID, pcm, sample_rate=16000))
    encoded = base64.b64decode(message["media"]["payload"])

    assert message["event"] == "media"
    assert message["streamSid"] == STREAM_SID
    assert len(encoded) == 160
    assert rms_energy(decode_mulaw(encoded)) > 5000


def test_stream_parser_validates_format_audio_mark_dtmf_and_stop() -> None:
    start = parse_stream_message(
        json.dumps(
            {
                "event": "start",
                "start": {
                    "streamSid": STREAM_SID,
                    "callSid": CALL_SID,
                    "mediaFormat": {
                        "encoding": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "channels": 1,
                    },
                    "customParameters": {"agent": "clinic"},
                },
            }
        )
    )
    frame = parse_stream_message(
        json.dumps(
            {
                "event": "media",
                "media": {
                    "chunk": "7",
                    "timestamp": "120",
                    "payload": base64.b64encode(bytes([0xFF]) * 160).decode(),
                },
            }
        )
    )
    mark = parse_stream_message(
        json.dumps(
            {
                "event": "mark",
                "streamSid": STREAM_SID,
                "mark": {"name": "turn_1"},
            }
        )
    )
    dtmf = parse_stream_message(
        json.dumps(
            {
                "event": "dtmf",
                "streamSid": STREAM_SID,
                "dtmf": {"digit": "#"},
            }
        )
    )
    stop = parse_stream_message(json.dumps({"event": "stop", "streamSid": STREAM_SID}))

    assert isinstance(start, MediaEvent)
    assert start.custom_parameters == {"agent": "clinic"}
    assert frame == MediaFrame(sequence=7, timestamp_ms=120, payload=bytes([0xFF]) * 160)
    assert isinstance(mark, MediaEvent)
    assert mark.name == "turn_1"
    assert isinstance(dtmf, MediaEvent)
    assert dtmf.digit == "#"
    assert isinstance(stop, MediaEvent)
    assert stop.type == "stop"


@pytest.mark.parametrize(
    "message",
    [
        "{",
        json.dumps({"event": "unknown"}),
        json.dumps(
            {
                "event": "start",
                "start": {
                    "streamSid": STREAM_SID,
                    "callSid": CALL_SID,
                    "mediaFormat": {
                        "encoding": "audio/opus",
                        "sampleRate": 48000,
                        "channels": 2,
                    },
                },
            }
        ),
        json.dumps(
            {
                "event": "media",
                "media": {"chunk": 1, "timestamp": 0, "payload": "not-base64"},
            }
        ),
    ],
)
def test_invalid_stream_messages_are_cataloged(message: str) -> None:
    with pytest.raises(VoiceyError) as caught:
        parse_stream_message(message)

    assert caught.value.code == "VY-TEL-010"


def test_jitter_buffer_reorders_within_tolerance_and_skips_late_gap() -> None:
    buffer = JitterBuffer(max_late_frames=2)
    payload = bytes([0xFF]) * 160

    assert buffer.push(MediaFrame(1, 0, payload))[0].sequence == 1
    assert buffer.push(MediaFrame(3, 40, payload)) == ()
    emitted = buffer.push(MediaFrame(2, 20, payload))
    assert [frame.sequence for frame in emitted] == [2, 3]

    assert buffer.push(MediaFrame(5, 80, payload)) == ()
    assert buffer.push(MediaFrame(6, 100, payload)) == ()
    skipped = buffer.push(MediaFrame(7, 120, payload))
    assert [frame.sequence for frame in skipped] == [5, 6, 7]
    assert buffer.push(MediaFrame(4, 60, payload)) == ()
    assert buffer.flush() == ()

    buffered = JitterBuffer(max_late_frames=5)
    buffered.push(MediaFrame(10, 0, payload))
    buffered.push(MediaFrame(12, 40, payload))
    assert [frame.sequence for frame in buffered.flush()] == [12]

    with pytest.raises(VoiceyError) as tolerance:
        JitterBuffer(max_late_frames=-1)
    with pytest.raises(VoiceyError) as frame:
        buffer.push(MediaFrame(13, 0, b"short"))
    assert tolerance.value.code == "VY-TEL-010"
    assert frame.value.code == "VY-TEL-010"


@pytest.mark.asyncio
async def test_frame_pacing_uses_20ms_deadlines_and_resets_after_stall() -> None:
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
    assert sleeps[0] == pytest.approx(0.02)
    now += 0.1
    assert await pacer.wait() == 0


def test_mark_clear_interruption_flush_is_immediate_and_observable() -> None:
    now = 2.0
    tracker = PlaybackTracker(clock=lambda: now)
    tracker.sent("turn_1")
    tracker.sent("turn_2")
    now += 0.025

    assert tracker.acknowledged("turn_1") == pytest.approx(25)
    assert tracker.cleared() == ("turn_2",)
    assert json.loads(mark_message(STREAM_SID, "turn_3"))["event"] == "mark"
    assert json.loads(clear_message(STREAM_SID)) == {
        "event": "clear",
        "streamSid": STREAM_SID,
    }

    tracker.sent("turn_3")
    with pytest.raises(VoiceyError) as duplicate:
        tracker.sent("turn_3")
    with pytest.raises(VoiceyError) as unknown:
        tracker.acknowledged("missing")
    assert duplicate.value.code == "VY-TEL-010"
    assert unknown.value.code == "VY-TEL-010"


def test_media_helper_boundaries_fail_closed() -> None:
    with pytest.raises(VoiceyError) as pacer:
        FramePacer(frame_duration_s=0)
    with pytest.raises(VoiceyError) as ulaw:
        mulaw_to_linear16(256)
    with pytest.raises(VoiceyError) as pcm:
        linear16_to_mulaw(40000)
    with pytest.raises(VoiceyError) as rates:
        resample_linear([0], source_rate=0, target_rate=8000)
    with pytest.raises(VoiceyError) as sample:
        resample_linear([40000], source_rate=8000, target_rate=16000)
    with pytest.raises(VoiceyError) as tone:
        tone_pcm16(
            frequency_hz=9000,
            sample_rate=16000,
            duration_s=1,
        )
    with pytest.raises(VoiceyError) as frequency:
        dominant_frequency([0], sample_rate=8000)
    with pytest.raises(VoiceyError) as mark:
        mark_message(STREAM_SID, "not valid")
    assert resample_linear([], source_rate=8000, target_rate=16000) == ()
    assert rms_energy([]) == 0
    assert {
        pacer.value.code,
        ulaw.value.code,
        pcm.value.code,
        rates.value.code,
        sample.value.code,
        tone.value.code,
        frequency.value.code,
        mark.value.code,
    } == {"VY-TEL-010"}
