import pytest

from voicekit.errors import ERROR_CATALOG, VoicekitError


def test_registered_error_exposes_stable_code_and_fix() -> None:
    error = VoicekitError("VK-RES-004", detail="event evt_test")

    assert error.code == "VK-RES-004"
    assert error.definition.fix
    assert "event evt_test" in str(error)


def test_unregistered_error_code_is_a_bug() -> None:
    with pytest.raises(AssertionError, match="unregistered voicekit error code"):
        VoicekitError("VK-NOT-REGISTERED")


def test_catalog_keys_match_definitions() -> None:
    assert all(key == definition.code for key, definition in ERROR_CATALOG.items())
