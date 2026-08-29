from voicey import __version__


def test_package_version_matches_stable_release() -> None:
    assert __version__ == "1.0.0"
