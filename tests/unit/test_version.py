from voicekit import __version__


def test_version_is_pre_release_during_build() -> None:
    assert __version__ == "0.0.0.dev0"
