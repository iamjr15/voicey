from typer.testing import CliRunner

from voicekit import __version__
from voicekit.cli.app import app

runner = CliRunner()


def test_bare_command_prints_status_and_next_step() -> None:
    result = runner.invoke(app)

    assert result.exit_code == 0
    assert "voicekit is installed" in result.stdout
    assert "Next:" in result.stdout


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__
