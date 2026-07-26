"""Top-level command during the P0 bootstrap."""

from typing import Annotated

import typer
from rich.console import Console

from voicekit import __version__

app = typer.Typer(
    add_completion=False,
    help="Build, run, test, and deploy native Pipecat or LiveKit voice agents.",
    no_args_is_help=False,
)
console = Console()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", help="Print the installed voicekit version."),
    ] = False,
) -> None:
    """Show project status or dispatch a voicekit command."""
    if version:
        console.print(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print("[bold]voicekit[/bold] is installed.")
        console.print("Current build phase: P0")
        console.print("Next: follow docs/PROGRESS.md while the guided init command is built.")
