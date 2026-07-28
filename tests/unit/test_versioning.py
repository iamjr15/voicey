from __future__ import annotations

import asyncio

import pytest

from voicekit.versioning import (
    Deprecation,
    SemVer,
    VoicekitDeprecationWarning,
    deprecated,
    validate_deprecations,
    warn_deprecated,
)


@pytest.mark.parametrize(
    "raw",
    [
        "0.0.0",
        "1.2.3",
        "1.2.3-rc.1",
        "1.2.3-alpha.beta+build.7",
    ],
)
def test_semver_accepts_valid_versions(raw: str) -> None:
    assert SemVer.parse(raw).public == tuple(int(part) for part in raw.split("-", 1)[0].split("."))


@pytest.mark.parametrize(
    "raw",
    ["1", "1.2", "01.2.3", "1.02.3", "1.2.03", "1.2.3-", "1.2.3-01", "v1.2.3"],
)
def test_semver_rejects_invalid_versions(raw: str) -> None:
    with pytest.raises(ValueError, match="Semantic Versioning"):
        SemVer.parse(raw)


def _deprecation(*, remove_in: str = "1.4.0") -> Deprecation:
    return Deprecation(
        name="voicekit.old",
        since="1.2.0",
        remove_in=remove_in,
        replacement="voicekit.new",
        migration_url="https://voicekit.dev/docs/migrate-old",
    )


def test_deprecation_requires_two_minor_releases() -> None:
    assert _deprecation().remove_in == "1.4.0"
    with pytest.raises(ValueError, match="two-minor"):
        _deprecation(remove_in="1.3.9")
    with pytest.raises(ValueError, match="stable"):
        Deprecation(
            name="voicekit.old",
            since="1.2.0-rc.1",
            remove_in="1.4.0",
            replacement="voicekit.new",
            migration_url="https://voicekit.dev/docs/migrate-old",
        )


def test_warn_deprecated_has_actionable_message() -> None:
    with pytest.warns(
        VoicekitDeprecationWarning,
        match=r"deprecated since Voicekit 1\.2\.0.*voicekit\.new",
    ):
        warn_deprecated(_deprecation())


def test_deprecated_wraps_sync_and_async_callables() -> None:
    @deprecated(_deprecation())
    def old_sync(value: int) -> int:
        """Preserved metadata."""
        return value + 1

    @deprecated(_deprecation())
    async def old_async(value: int) -> int:
        return value + 2

    with pytest.warns(VoicekitDeprecationWarning):
        assert old_sync(1) == 2
    with pytest.warns(VoicekitDeprecationWarning):
        assert asyncio.run(old_async(1)) == 3
    assert old_sync.__name__ == "old_sync"
    assert old_sync.__doc__ == "Preserved metadata."


def test_release_policy_rejects_overdue_deprecation() -> None:
    validate_deprecations("1.3.99", registry=(_deprecation(),))
    with pytest.raises(ValueError, match="reached removal"):
        validate_deprecations("1.4.0", registry=(_deprecation(),))
