"""Pick-one carrier adapter registry backed by Python entry points."""

from __future__ import annotations

from importlib.metadata import EntryPoint, entry_points
from typing import Any

from voicekit.errors import VoicekitError
from voicekit.telephony.protocol import TelephonyAdapter

ENTRY_POINT_GROUP = "voicekit.telephony"


def adapter_names() -> tuple[str, ...]:
    """Return installed adapter names without importing provider SDKs."""
    names = [point.name for point in entry_points(group=ENTRY_POINT_GROUP)]
    return tuple(sorted(set(names)))


def load_adapter(name: str, **kwargs: Any) -> TelephonyAdapter:
    """Load exactly one named adapter and construct it with explicit settings."""
    matches = [point for point in entry_points(group=ENTRY_POINT_GROUP) if point.name == name]
    if len(matches) != 1:
        detail = (
            f"adapter {name!r} is not installed."
            if not matches
            else f"adapter {name!r} is registered more than once."
        )
        raise VoicekitError("VK-TEL-001", detail=detail)
    return _construct(matches[0], kwargs)


def _construct(point: EntryPoint, kwargs: dict[str, Any]) -> TelephonyAdapter:
    try:
        factory = point.load()
        adapter = factory(**kwargs)
    except ImportError as exc:
        raise VoicekitError(
            "VK-TEL-001",
            detail=f"adapter {point.name!r} is missing its optional dependency.",
        ) from exc
    except VoicekitError:
        raise
    except Exception as exc:
        raise VoicekitError(
            "VK-TEL-002",
            detail=f"adapter {point.name!r} could not be initialized.",
        ) from exc
    if not isinstance(adapter, TelephonyAdapter):
        raise VoicekitError(
            "VK-TEL-001",
            detail=f"entry point {point.name!r} does not implement TelephonyAdapter.",
        )
    return adapter
