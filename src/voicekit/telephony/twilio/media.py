"""Twilio Media Streams protocol and codec certification helpers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import re
import struct
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Literal, cast

from voicekit.errors import VoicekitError

_STREAM_SID = re.compile(r"^MZ[0-9a-fA-F]{32}$")
_CALL_SID = re.compile(r"^CA[0-9a-fA-F]{32}$")
_MARK_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635


@dataclass(frozen=True, slots=True)
class MediaFrame:
    """One decoded inbound 20ms μ-law frame."""

    sequence: int
    timestamp_ms: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class MediaEvent:
    """Validated non-audio stream event."""

    type: Literal["connected", "start", "mark", "dtmf", "stop"]
    stream_sid: str | None = None
    call_sid: str | None = None
    name: str | None = None
    digit: str | None = None
    custom_parameters: dict[str, str] | None = None


class JitterBuffer:
    """Small reordering buffer that skips a missing frame only after tolerance."""

    def __init__(self, *, max_late_frames: int = 3) -> None:
        if max_late_frames < 0:
            raise VoicekitError("VK-TEL-010", detail="jitter tolerance cannot be negative.")
        self.max_late_frames = max_late_frames
        self._expected: int | None = None
        self._buffer: dict[int, MediaFrame] = {}

    def push(self, frame: MediaFrame) -> tuple[MediaFrame, ...]:
        """Insert a frame and return every newly contiguous frame."""
        if frame.sequence < 0 or frame.timestamp_ms < 0 or len(frame.payload) != 160:
            raise VoicekitError("VK-TEL-010", detail="invalid Twilio 20ms media frame.")
        if self._expected is None:
            self._expected = frame.sequence
        if frame.sequence < self._expected or frame.sequence in self._buffer:
            return ()
        self._buffer[frame.sequence] = frame
        if self._expected not in self._buffer and len(self._buffer) > self.max_late_frames:
            self._expected = min(self._buffer)
        ready: list[MediaFrame] = []
        while self._expected in self._buffer:
            ready.append(self._buffer.pop(self._expected))
            self._expected += 1
        return tuple(ready)

    def flush(self) -> tuple[MediaFrame, ...]:
        """Return remaining frames in order at stream termination."""
        frames = tuple(self._buffer[key] for key in sorted(self._buffer))
        self._buffer.clear()
        if frames:
            self._expected = frames[-1].sequence + 1
        return frames


class FramePacer:
    """Monotonic 20ms outbound pacing without accumulating long stalls."""

    def __init__(
        self,
        *,
        frame_duration_s: float = 0.02,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if frame_duration_s <= 0:
            raise VoicekitError("VK-TEL-010", detail="frame duration must be positive.")
        self.frame_duration_s = frame_duration_s
        self._clock = clock
        self._sleep = sleep
        self._next_deadline: float | None = None

    async def wait(self) -> float:
        """Wait until the next send deadline and return actual delay seconds."""
        now = self._clock()
        if self._next_deadline is None:
            self._next_deadline = now
        if now - self._next_deadline > self.frame_duration_s:
            self._next_deadline = now
        delay = max(0.0, self._next_deadline - now)
        if delay:
            await self._sleep(delay)
        self._next_deadline += self.frame_duration_s
        return delay


class PlaybackTracker:
    """Track mark acknowledgements and interruption clear semantics."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._pending: dict[str, float] = {}

    def sent(self, name: str) -> None:
        if not _MARK_NAME.fullmatch(name) or name in self._pending:
            raise VoicekitError("VK-TEL-010", detail="invalid or duplicate playback mark.")
        self._pending[name] = self._clock()

    def acknowledged(self, name: str) -> float:
        try:
            started = self._pending.pop(name)
        except KeyError as exc:
            raise VoicekitError(
                "VK-TEL-010", detail="unknown playback mark acknowledgement."
            ) from exc
        return max(0.0, (self._clock() - started) * 1000)

    def cleared(self) -> tuple[str, ...]:
        """Forget audio flushed by a Twilio clear message."""
        names = tuple(self._pending)
        self._pending.clear()
        return names


def parse_stream_message(raw: str | bytes) -> MediaFrame | MediaEvent:
    """Validate one inbound Twilio JSON message without trusting its shape."""
    try:
        decoded = json.loads(raw)
        message = cast("dict[str, Any]", decoded)
        event = str(message["event"])
        if event == "connected":
            return MediaEvent(type="connected")
        if event == "start":
            start = cast("dict[str, Any]", message["start"])
            media_format = cast("dict[str, Any]", start["mediaFormat"])
            if (
                media_format.get("encoding") != "audio/x-mulaw"
                or int(media_format.get("sampleRate", 0)) != 8000
                or int(media_format.get("channels", 0)) != 1
            ):
                raise VoicekitError(
                    "VK-TEL-010",
                    detail="Twilio stream is not mono μ-law at 8kHz.",
                )
            parameters = {
                str(key): str(value)
                for key, value in cast(
                    "dict[object, object]",
                    start.get("customParameters", {}),
                ).items()
            }
            return MediaEvent(
                type="start",
                stream_sid=_validate_stream_sid(str(start["streamSid"])),
                call_sid=_validate_call_sid(str(start["callSid"])),
                custom_parameters=parameters,
            )
        if event == "media":
            media = cast("dict[str, Any]", message["media"])
            payload = base64.b64decode(str(media["payload"]), validate=True)
            return MediaFrame(
                sequence=int(media["chunk"]),
                timestamp_ms=int(media["timestamp"]),
                payload=payload,
            )
        stream_sid = _validate_stream_sid(str(message["streamSid"]))
        if event == "mark":
            mark = cast("dict[str, Any]", message["mark"])
            return MediaEvent(
                type="mark",
                stream_sid=stream_sid,
                name=_validate_mark(str(mark["name"])),
            )
        if event == "dtmf":
            dtmf = cast("dict[str, Any]", message["dtmf"])
            digit = str(dtmf["digit"])
            if digit not in "0123456789*#":
                raise VoicekitError("VK-TEL-010", detail="invalid inbound DTMF digit.")
            return MediaEvent(type="dtmf", stream_sid=stream_sid, digit=digit)
        if event == "stop":
            return MediaEvent(type="stop", stream_sid=stream_sid)
    except VoicekitError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, binascii.Error) as exc:
        raise VoicekitError("VK-TEL-010", detail="malformed Twilio media message.") from exc
    raise VoicekitError("VK-TEL-010", detail=f"unsupported Twilio media event {event!r}.")


def media_message(stream_sid: str, pcm16: Sequence[int], *, sample_rate: int) -> str:
    """Build one outbound μ-law media message from signed 16-bit samples."""
    samples = resample_linear(pcm16, source_rate=sample_rate, target_rate=8000)
    payload = bytes(linear16_to_mulaw(sample) for sample in samples)
    return _json_message(
        {
            "event": "media",
            "streamSid": _validate_stream_sid(stream_sid),
            "media": {"payload": base64.b64encode(payload).decode()},
        }
    )


def mark_message(stream_sid: str, name: str) -> str:
    return _json_message(
        {
            "event": "mark",
            "streamSid": _validate_stream_sid(stream_sid),
            "mark": {"name": _validate_mark(name)},
        }
    )


def clear_message(stream_sid: str) -> str:
    return _json_message(
        {
            "event": "clear",
            "streamSid": _validate_stream_sid(stream_sid),
        }
    )


def mulaw_to_linear16(value: int) -> int:
    """Decode one ITU-T G.711 μ-law byte."""
    if not 0 <= value <= 255:
        raise VoicekitError("VK-TEL-010", detail="μ-law byte is outside 0..255.")
    inverted = (~value) & 0xFF
    magnitude = ((inverted & 0x0F) << 3) + _ULAW_BIAS
    magnitude <<= (inverted & 0x70) >> 4
    return _ULAW_BIAS - magnitude if inverted & 0x80 else magnitude - _ULAW_BIAS


def linear16_to_mulaw(sample: int) -> int:
    """Encode one signed 16-bit sample as ITU-T G.711 μ-law."""
    if not -32768 <= sample <= 32767:
        raise VoicekitError("VK-TEL-010", detail="PCM sample is outside signed 16-bit range.")
    sign = 0x80 if sample < 0 else 0
    magnitude = min(abs(sample), _ULAW_CLIP) + _ULAW_BIAS
    exponent = max(0, min(7, magnitude.bit_length() - 8))
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def decode_mulaw(payload: bytes) -> tuple[int, ...]:
    return tuple(mulaw_to_linear16(value) for value in payload)


def resample_linear(
    samples: Sequence[int],
    *,
    source_rate: int,
    target_rate: int,
) -> tuple[int, ...]:
    """Deterministically resample mono PCM for codec-loopback assertions."""
    if source_rate <= 0 or target_rate <= 0:
        raise VoicekitError("VK-TEL-010", detail="sample rates must be positive.")
    if not samples:
        return ()
    if any(not -32768 <= sample <= 32767 for sample in samples):
        raise VoicekitError("VK-TEL-010", detail="PCM sample is outside signed 16-bit range.")
    target_length = max(1, round(len(samples) * target_rate / source_rate))
    output: list[int] = []
    for index in range(target_length):
        position = index * source_rate / target_rate
        lower = min(int(position), len(samples) - 1)
        upper = min(lower + 1, len(samples) - 1)
        fraction = position - lower
        output.append(round(samples[lower] * (1 - fraction) + samples[upper] * fraction))
    return tuple(output)


def tone_pcm16(
    *,
    frequency_hz: float,
    sample_rate: int,
    duration_s: float,
    amplitude: int = 12000,
) -> tuple[int, ...]:
    """Generate a deterministic mono tone for the carrier audio loopback rig."""
    if (
        frequency_hz <= 0
        or sample_rate <= 0
        or duration_s <= 0
        or not 0 < amplitude <= 32767
        or frequency_hz >= sample_rate / 2
    ):
        raise VoicekitError("VK-TEL-010", detail="invalid certification tone settings.")
    count = round(sample_rate * duration_s)
    return tuple(
        round(amplitude * math.sin(2 * math.pi * frequency_hz * index / sample_rate))
        for index in range(count)
    )


def rms_energy(samples: Sequence[int]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def dominant_frequency(samples: Sequence[int], *, sample_rate: int) -> float:
    """Estimate tone frequency by positive-going zero crossings."""
    if len(samples) < 2 or sample_rate <= 0:
        raise VoicekitError("VK-TEL-010", detail="not enough PCM for frequency detection.")
    crossings = sum(1 for previous, current in pairwise(samples) if previous <= 0 < current)
    duration = (len(samples) - 1) / sample_rate
    return crossings / duration


def pcm16le(samples: Sequence[int]) -> bytes:
    if any(not -32768 <= sample <= 32767 for sample in samples):
        raise VoicekitError("VK-TEL-010", detail="PCM sample is outside signed 16-bit range.")
    return struct.pack(f"<{len(samples)}h", *samples)


def _validate_stream_sid(value: str) -> str:
    if not _STREAM_SID.fullmatch(value):
        raise VoicekitError("VK-TEL-010", detail="invalid Twilio StreamSid.")
    return value


def _validate_call_sid(value: str) -> str:
    if not _CALL_SID.fullmatch(value):
        raise VoicekitError("VK-TEL-010", detail="invalid Twilio CallSid.")
    return value


def _validate_mark(value: str) -> str:
    if not _MARK_NAME.fullmatch(value):
        raise VoicekitError("VK-TEL-010", detail="invalid Twilio playback mark.")
    return value


def _json_message(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
