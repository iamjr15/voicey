"""Production command rail for voicekit projects."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn, TypeVar, cast
from urllib.parse import quote

import typer
from rich.console import Console
from rich.table import Table

from voicekit import __version__
from voicekit.capabilities import DEFAULT_CAPABILITIES
from voicekit.cli.context import (
    ProjectContext,
    discover_project,
    next_step,
    require_manifest,
)
from voicekit.cli.doctor import Doctor, DoctorCheck
from voicekit.cli.environment import EnvFileStore, ensure_env_ignored
from voicekit.cli.keys import ProviderKeyValidator, mask_value, required_entries
from voicekit.cli.prompts import PromptChoice, QuestionaryPromptIO
from voicekit.cli.wizard import InitOptions, InitWizard
from voicekit.config.catalog import DEFAULT_PROVIDER_CATALOG, ProviderCatalogEntry
from voicekit.errors import ERROR_CATALOG, VoicekitError, error_docs_url
from voicekit.obs.logging import scrub_secrets
from voicekit.recipes.registry import DEFAULT_RECIPE_REGISTRY
from voicekit.storage.sqlite import SQLiteRepository
from voicekit.telephony.models import PipecatTarget, RollbackToken
from voicekit.tunnel import TunnelPreference

if TYPE_CHECKING:
    from voicekit.telephony.twilio import TwilioAdapter

app = typer.Typer(
    add_completion=False,
    help="Build, run, test, and deploy native Pipecat or LiveKit voice agents.",
    no_args_is_help=False,
    pretty_exceptions_enable=False,
)
numbers_app = typer.Typer(help="List, buy, release, point, or restore phone numbers.")
keys_app = typer.Typer(help="Collect, mask, and live-validate provider credentials.")
calls_app = typer.Typer(help="Inspect calls and redeliver immutable result events.")
recipes_app = typer.Typer(help="List and copy versioned native-workflow recipes.")
deploy_app = typer.Typer(help="Generate and operate a supported deployment target.")
app.add_typer(numbers_app, name="numbers")
app.add_typer(keys_app, name="keys")
app.add_typer(calls_app, name="calls")
app.add_typer(recipes_app, name="recipes")
app.add_typer(deploy_app, name="deploy")

console = Console()
stderr = Console(stderr=True)
ReturnT = TypeVar("ReturnT")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", help="Print the installed voicekit version."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable project status."),
    ] = False,
) -> None:
    """Show project status or dispatch a voicekit command."""
    if version:
        console.print(__version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is not None:
        return
    context = _context()
    if json_output:
        _json(
            {
                "installed_version": __version__,
                "project": (
                    None if context.manifest is None else context.manifest.model_dump(mode="json")
                ),
                "checkpoint": context.checkpoint,
                "next_step": next_step(context),
            }
        )
        return
    if context.manifest is not None:
        console.print(
            f"[bold]{context.manifest.project_name}[/bold] · "
            f"{context.manifest.runtime} · {context.manifest.recipe.name}"
        )
    elif context.checkpoint:
        console.print("[bold]voicekit[/bold] setup is incomplete.")
    else:
        console.print("[bold]voicekit[/bold] is installed.")
    console.print(f"Next: {next_step(context)}")


@app.command("init")
def init_command(
    path: Annotated[
        Path,
        typer.Argument(help="Project directory to create or resume."),
    ] = Path("."),
    name: Annotated[str | None, typer.Option("--name", help="Project/package name.")] = None,
    recipe: Annotated[str | None, typer.Option("--recipe", help="Recipe id.")] = None,
    description: Annotated[
        str | None,
        typer.Option("--description", help="Scratch-agent purpose."),
    ] = None,
    channels: Annotated[
        str | None,
        typer.Option("--channels", help="Comma-separated phone,web choices."),
    ] = None,
    phone_provider: Annotated[
        str | None,
        typer.Option("--phone-provider", help="Carrier id."),
    ] = None,
    phone_number: Annotated[
        str | None,
        typer.Option("--phone-number", help="Owned E.164 number."),
    ] = None,
    runtime: Annotated[str | None, typer.Option("--runtime", help="Runtime id.")] = None,
    models: Annotated[
        list[str] | None,
        typer.Option(
            "--models",
            help="Model assignments: stt=id,llm=id,tts=id (repeatable).",
        ),
    ] = None,
    draft_prompts: Annotated[
        bool | None,
        typer.Option(
            "--draft-prompts/--no-draft-prompts",
            help="Explicitly opt in/out of one paid LLM drafting request.",
        ),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Resume the secret-free setup checkpoint."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Non-interactive mode; never chooses substantive answers.",
        ),
    ] = False,
) -> None:
    """Create a working native-workflow project with live-validated keys."""

    async def operation() -> None:
        prompt = QuestionaryPromptIO(interactive=sys.stdin.isatty() and not yes)
        result = await InitWizard(prompt=prompt).run(
            path,
            InitOptions(
                project_name=name,
                recipe=recipe,
                description=description,
                channels=_csv_tuple(channels),
                phone_provider=phone_provider,
                phone_number=phone_number,
                runtime=runtime,
                models=_model_assignments(models),
                draft_prompts=draft_prompts,
                resume=resume,
            ),
        )
        console.print(f"Created [bold]{result.manifest.project_name}[/bold].")
        console.print(f"Files: {len(result.written)}")
        console.print(f"Next: cd {result.project_dir} && voicekit dev")

    _guard_async(operation)


@app.command("dev")
def dev_command(
    phone: Annotated[
        bool,
        typer.Option("--phone/--no-phone", help="Temporarily route the selected number."),
    ] = False,
    tunnel: Annotated[
        str,
        typer.Option("--tunnel", help="auto, cloudflared, ngrok, or url."),
    ] = "auto",
    public_url: Annotated[
        str | None,
        typer.Option("--url", help="Operator-owned HTTPS URL when --tunnel url."),
    ] = None,
    port: Annotated[int, typer.Option("--port", help="Public-listener local port.")] = 7860,
    no_open: Annotated[
        bool,
        typer.Option("--no-open", help="Do not open the browser playground."),
    ] = False,
) -> None:
    """Run the local agent; Ctrl-C restores any temporary phone route."""
    del no_open
    if tunnel not in {"auto", "cloudflared", "ngrok", "url"}:
        _fail(VoicekitError("VK-CLI-010", detail=f"unknown tunnel {tunnel!r}."))

    async def operation() -> None:
        from voicekit.cli.dev import run_dev

        await run_dev(
            _context(),
            phone=phone,
            tunnel=cast("TunnelPreference", tunnel),
            public_url=public_url,
            port=port,
            notice=console.print,
        )
        console.print("Next: voicekit calls list")

    _guard_async(operation)


@app.command("call")
def call_command(
    e164: Annotated[str, typer.Argument(help="Destination E.164 number.")],
    public_url: Annotated[
        str | None,
        typer.Option("--url", help="Running agent's public HTTPS base."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm the paid outbound call."),
    ] = False,
) -> None:
    """Place one paid test call through the durable Twilio intent ledger."""

    def operation() -> None:
        context = _context()
        manifest = require_manifest(context)
        if manifest.phone_number is None:
            raise VoicekitError("VK-CLI-007", detail="this project has no phone number.")
        _confirm(
            f"Place a paid call from {manifest.phone_number} to {e164}?",
            yes=yes,
        )
        target_url = public_url or context.environment.get("VOICEKIT_PUBLIC_URL")
        if not target_url:
            raise VoicekitError(
                "VK-CLI-007",
                detail="start `voicekit dev --phone` or pass --url first.",
            )
        adapter = _twilio(context)
        try:
            call_sid = adapter.start_call(
                manifest.phone_number,
                e164,
                PipecatTarget(target_url),
            )
        finally:
            adapter.ledger.close()
        console.print(f"Call started: {call_sid}")
        console.print("Next: voicekit calls list")

    _guard(operation)


@app.command("doctor")
def doctor_command(
    fix: Annotated[
        bool,
        typer.Option("--fix/--no-fix", help="Apply the documented safe subset."),
    ] = False,
    send_test: Annotated[
        bool,
        typer.Option("--send-test/--no-send-test", help="POST a signed receiver test."),
    ] = False,
    port: Annotated[int, typer.Option("--port", help="Port to test for availability.")] = 7860,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit one machine-readable report."),
    ] = False,
) -> None:
    """Run every local, provider, carrier, delivery, and capacity preflight."""

    async def operation() -> None:
        doctor = Doctor(_context(), port=port, send_test=send_test)
        fixed = doctor.apply_safe_fixes() if fix else ()

        def stream(check: DoctorCheck) -> None:
            if json_output:
                return
            mark = "[green]✔[/green]" if check.ok else "[red]✖[/red]"
            console.print(f"{mark} {check.description}")
            for issue in check.issues:
                console.print(f"  Issue: {issue}")
            for advice in check.advice:
                console.print(f"  Advice: {advice}")

        report = await doctor.run(on_check=stream)
        payload = {
            "ok": report.ok,
            "fixed": list(fixed),
            "checks": [asdict(check) for check in report.checks],
            "next_step": "voicekit dev" if report.ok else "voicekit doctor",
        }
        if json_output:
            _json(payload)
        else:
            console.print(f"Next: {payload['next_step']}")
        if not report.ok:
            raise typer.Exit(code=1)

    _guard_async(operation, json_output=json_output)


@keys_app.command("list")
def keys_list(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show masked selected keys with a fresh authenticated validation."""

    async def operation() -> None:
        context = _context()
        manifest = require_manifest(context)
        validator = ProviderKeyValidator()
        entries = required_entries(
            cast("dict[str, str]", manifest.models),
            carrier=manifest.carriers[0] if manifest.carriers else None,
        )
        rows: list[dict[str, object]] = []
        for entry in entries:
            check = await validator.validate(
                entry.kind,
                entry.id,
                context.environment,
            )
            rows.append(
                {
                    "provider": check.provider,
                    "status": check.status,
                    "keys": {
                        name: mask_value(context.environment.get(name, ""))
                        for name in check.env_names
                    },
                    "detail": check.detail,
                }
            )
        _rows_or_table(
            rows,
            columns=("provider", "status", "keys"),
            json_output=json_output,
            next_command="voicekit keys validate",
        )

    _guard_async(operation, json_output=json_output)


@keys_app.command("validate")
def keys_validate(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Re-run authenticated read checks for every selected provider."""
    keys_list(json_output=json_output)


@keys_app.command("add")
def keys_add(
    provider: Annotated[str, typer.Argument(help="Provider id, for example deepgram.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Use values already injected into the environment."),
    ] = False,
) -> None:
    """Paste/rotate provider credentials, validate, then write owner-only `.env`."""

    async def operation() -> None:
        context = _context()
        entry = _provider_entry(context, provider)
        prompt = QuestionaryPromptIO(interactive=sys.stdin.isatty() and not yes)
        values = dict(context.environment)
        pasted: dict[str, str] = {}
        if prompt.interactive:
            pasted = {name: prompt.secret(f"Paste {name}:") for name in entry.key_env_vars}
            values.update(pasted)
        check = await ProviderKeyValidator().validate(entry.kind, entry.id, values)
        if check.status != "valid":
            raise VoicekitError(
                "VK-CLI-004",
                detail=f"{check.detail} {check.fix}",
            )
        if pasted:
            ensure_env_ignored(context.root)
            EnvFileStore(context.root / ".env").update(pasted)
        console.print(f"✓ {provider} credentials validated.")
        console.print("Next: voicekit doctor")

    _guard_async(operation)


@recipes_app.command("list")
def recipes_list(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List packaged recipes and explicit availability facts."""

    def operation() -> None:
        rows: list[dict[str, object]] = []
        for recipe in DEFAULT_RECIPE_REGISTRY.list():
            capability = DEFAULT_CAPABILITIES.get("recipe", recipe.name)
            available = bool(
                capability is not None and capability.enabled and recipe.source_available
            )
            rows.append(
                {
                    "name": recipe.name,
                    "version": recipe.version,
                    "runtimes": sorted(recipe.runtimes),
                    "available": available,
                    "description": recipe.description,
                    "reason": (
                        None
                        if available
                        else (
                            capability.unavailable_reason
                            if capability is not None
                            else "not packaged"
                        )
                    ),
                }
            )
        _rows_or_table(
            rows,
            columns=("name", "version", "available", "description"),
            json_output=json_output,
            next_command="voicekit init --recipe scratch",
        )

    _guard(operation, json_output=json_output)


@recipes_app.command("add")
def recipes_add(
    name: Annotated[str, typer.Argument(help="Recipe id.")],
) -> None:
    """Copy the runtime-matching native recipe without overwriting project code."""

    def operation() -> None:
        manifest = require_manifest(_context())
        DEFAULT_RECIPE_REGISTRY.require(name, manifest.runtime)
        raise VoicekitError(
            "VK-CLI-005",
            detail=f"recipe {name!r} copy support is not packaged in this build.",
        )

    _guard(operation)


@recipes_app.command("update-check")
def recipes_update_check(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Report recipe source drift without overwriting local changes."""
    _fail(
        VoicekitError("VK-CLI-005", detail="recipe drift tooling lands in P4."),
        json_output=json_output,
    )


@numbers_app.command("list")
def numbers_list(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List voice-capable numbers owned by the configured carrier."""

    def operation() -> None:
        adapter = _twilio(_context())
        try:
            rows: list[dict[str, object]] = []
            for number in adapter.list_numbers():
                row = cast("dict[str, object]", asdict(number))
                row["capabilities"] = sorted(number.capabilities)
                rows.append(row)
        finally:
            adapter.ledger.close()
        _rows_or_table(
            rows,
            columns=("number", "friendly_name", "country", "capabilities"),
            json_output=json_output,
            next_command="voicekit dev --phone",
        )

    _guard(operation, json_output=json_output)


@numbers_app.command("buy")
def numbers_buy(
    country: Annotated[str, typer.Argument(help="ISO-3166 alpha-2 country.")],
    area: Annotated[str | None, typer.Option("--area", help="Optional area code.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Confirm the purchase.")] = False,
) -> None:
    """Buy the first matching voice number after an explicit money confirmation."""

    def operation() -> None:
        _confirm(f"Buy one Twilio voice number in {country.upper()}/{area or '*'}?", yes=yes)
        adapter = _twilio(_context())
        try:
            number = adapter.buy_number(country, area)
        finally:
            adapter.ledger.close()
        console.print(f"Bought: {number.number}")
        console.print(f"Next: voicekit numbers point {number.number} --yes")

    _guard(operation)


@numbers_app.command("release")
def numbers_release(
    number: Annotated[str, typer.Argument(help="Owned E.164 number.")],
    yes: Annotated[bool, typer.Option("--yes", help="Confirm irreversible release.")] = False,
) -> None:
    """Release a carrier number after an explicit confirmation."""

    def operation() -> None:
        _confirm(f"Release {number}? The number may not be recoverable.", yes=yes)
        adapter = _twilio(_context())
        try:
            adapter.release_number(number)
        finally:
            adapter.ledger.close()
        console.print(f"Released: {number}")
        console.print("Next: voicekit numbers list")

    _guard(operation)


@numbers_app.command("point")
def numbers_point(
    number: Annotated[str | None, typer.Argument(help="Owned E.164 number.")] = None,
    public_url: Annotated[
        str | None,
        typer.Option("--url", help="Agent's public HTTPS base."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Confirm live route change.")] = False,
) -> None:
    """Point one production number and persist its complete rollback snapshot first."""

    def operation() -> None:
        context = _context()
        manifest = require_manifest(context)
        selected_number = number or manifest.phone_number
        target_url = public_url or context.environment.get("VOICEKIT_PUBLIC_URL")
        if selected_number is None or target_url is None:
            raise VoicekitError(
                "VK-CLI-007",
                detail="an owned number and --url/VOICEKIT_PUBLIC_URL are required.",
            )
        _confirm(f"Point live number {selected_number} to {target_url}?", yes=yes)
        adapter = _twilio(context, expected_public_base=target_url)
        try:
            token = adapter.point_inbound(selected_number, PipecatTarget(target_url))
        finally:
            adapter.ledger.close()
        console.print(f"Pointed {selected_number}. Rollback token: {token.token}")
        console.print(f"Next: voicekit numbers restore {token.token} --yes")

    _guard(operation)


@numbers_app.command("restore")
def numbers_restore(
    token: Annotated[str, typer.Argument(help="Persisted route rollback token.")],
    yes: Annotated[bool, typer.Option("--yes", help="Confirm live route restore.")] = False,
) -> None:
    """Compare-and-swap restore one previously captured carrier route."""

    def operation() -> None:
        _confirm(f"Restore the carrier route captured by {token}?", yes=yes)
        adapter = _twilio(_context())
        try:
            adapter.restore(RollbackToken(provider="twilio", token=token))
        finally:
            adapter.ledger.close()
        console.print(f"Restored: {token}")
        console.print("Next: voicekit numbers list")

    _guard(operation)


@calls_app.command("list")
def calls_list(
    undelivered: Annotated[
        bool,
        typer.Option("--undelivered/--all", help="Only pending/dead-letter deliveries."),
    ] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List protected local call records or undelivered events."""

    async def operation() -> None:
        path = _context().root / ".voicekit" / "calls.sqlite3"
        if not path.exists():
            rows: list[dict[str, object]] = []
        else:
            async with SQLiteRepository(path) as repository:
                if undelivered:
                    rows = [
                        delivery.model_dump(mode="json")
                        for delivery in await repository.list_deliveries(undelivered_only=True)
                    ][:limit]
                else:
                    rows = [
                        call.model_dump(mode="json")
                        for call in await repository.list_calls(limit=limit)
                    ]
        columns = (
            ("event_id", "call_id", "status", "attempt_count")
            if undelivered
            else ("call_id", "status", "channel", "started_at", "webhook_status")
        )
        _rows_or_table(
            rows,
            columns=columns,
            json_output=json_output,
            next_command="voicekit calls show <call-id>",
        )

    _guard_async(operation, json_output=json_output)


@calls_app.command("show")
def calls_show(
    call_id: Annotated[str, typer.Argument(help="Exact call id.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show one complete protected call timeline/transcript/tool record."""

    async def operation() -> None:
        path = _context().root / ".voicekit" / "calls.sqlite3"
        if not path.exists():
            raise VoicekitError("VK-OBS-003", detail=call_id)
        async with SQLiteRepository(path) as repository:
            record = await repository.get_call(call_id)
        payload = record.model_dump(mode="json")
        if json_output:
            _json({"call": payload, "next_step": "voicekit calls list"})
        else:
            console.print_json(data=payload)
            console.print("Next: voicekit calls list")

    _guard_async(operation, json_output=json_output)


@calls_app.command("redeliver")
def calls_redeliver(
    identifier: Annotated[str, typer.Argument(help="Event id or call id.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm a new receiver delivery attempt."),
    ] = False,
) -> None:
    """Reset delivery state while preserving immutable bytes and event id."""

    async def operation() -> None:
        _confirm(f"Redeliver immutable result {identifier}?", yes=yes)
        path = _context().root / ".voicekit" / "calls.sqlite3"
        if not path.exists():
            raise VoicekitError("VK-RES-009", detail=identifier)
        async with SQLiteRepository(path) as repository:
            try:
                event = await repository.get_event(identifier)
            except VoicekitError as exc:
                if exc.code != "VK-RES-009":
                    raise
                event = await repository.get_terminal_event_for_call(identifier)
            delivery = await repository.redeliver(event.event_id)
        console.print(f"Queued event {delivery.event_id} for redelivery.")
        console.print("Next: voicekit calls list --undelivered")

    _guard_async(operation)


@app.command("test")
def run_tests_command(
    filter_text: Annotated[str | None, typer.Option("--filter")] = None,
    audio: Annotated[bool, typer.Option("--audio/--no-audio")] = False,
    live: Annotated[bool, typer.Option("--live/--no-live")] = False,
    report: Annotated[str | None, typer.Option("--report")] = None,
) -> None:
    """Run unified simulated-caller scenarios (lands with runtime parity in P2)."""
    del filter_text, audio, live, report
    _fail(VoicekitError("VK-CLI-005", detail="unified `voicekit test` lands in P2."))


@deploy_app.callback(invoke_without_command=True)
def deploy_command(
    ctx: typer.Context,
    target: Annotated[str | None, typer.Argument(help="Deployment target.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Confirm external mutations.")] = False,
    skip_smoke: Annotated[
        bool,
        typer.Option("--skip-smoke/--smoke", help="Skip the post-deploy smoke call."),
    ] = False,
) -> None:
    """Deploy to a capability-gated target."""
    del yes, skip_smoke
    if ctx.invoked_subcommand is not None:
        return

    def operation() -> None:
        if target is None:
            raise VoicekitError("VK-CLI-001", detail="pass an explicit deploy target.")
        DEFAULT_CAPABILITIES.require("deploy", target)
        raise VoicekitError(
            "VK-CLI-005",
            detail=f"deploy target {target!r} is not packaged in this build.",
        )

    _guard(operation)


@app.command("upgrade")
def upgrade_command(
    pre: Annotated[bool, typer.Option("--pre/--stable")] = False,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Upgrade engine pins and report recipe drift (lands in P4)."""
    del pre, yes
    _fail(VoicekitError("VK-CLI-005", detail="upgrade tooling lands in P4."))


def _context() -> ProjectContext:
    return discover_project(Path.cwd(), dict(os.environ))


def _twilio(
    context: ProjectContext,
    *,
    expected_public_base: str | None = None,
) -> TwilioAdapter:
    from voicekit.telephony.twilio import TwilioAdapter

    manifest = require_manifest(context)
    if manifest.carriers != ["twilio"]:
        raise VoicekitError(
            "VK-CLI-005",
            detail="this command requires the enabled Twilio carrier.",
        )
    return TwilioAdapter(
        account_sid=context.environment.get("TWILIO_ACCOUNT_SID"),
        auth_token=context.environment.get("TWILIO_AUTH_TOKEN"),
        ledger_path=context.root / ".voicekit" / "telephony.sqlite3",
        expected_public_base=expected_public_base,
    )


def _provider_entry(context: ProjectContext, provider: str) -> ProviderCatalogEntry:
    manifest = require_manifest(context)
    entries = required_entries(
        cast("dict[str, str]", manifest.models),
        carrier=manifest.carriers[0] if manifest.carriers else None,
    )
    matching = [entry for entry in entries if entry.id.split("/", maxsplit=1)[0] == provider]
    if not matching:
        matching = [
            entry
            for entry in DEFAULT_PROVIDER_CATALOG.entries
            if entry.id.split("/", maxsplit=1)[0] == provider
        ]
    if not matching:
        raise VoicekitError("VK-CLI-005", detail=f"unknown provider {provider!r}.")
    return matching[0]


def _confirm(message: str, *, yes: bool) -> None:
    if yes:
        return
    prompt = QuestionaryPromptIO(interactive=sys.stdin.isatty())
    try:
        answer = prompt.select(
            message,
            (
                PromptChoice(
                    title="Proceed",
                    value="yes",
                    description="Apply the exact money/live change printed above.",
                ),
                PromptChoice(
                    title="Cancel",
                    value="no",
                    description="Leave provider state unchanged.",
                ),
            ),
        )
    except VoicekitError as exc:
        if exc.code != "VK-CLI-001":
            raise
        raise VoicekitError(
            "VK-CLI-008",
            detail=f"non-interactive live/money change requires --yes: {message}",
        ) from exc
    if answer != "yes":
        raise VoicekitError("VK-CLI-008", detail="operation cancelled; no change applied.")


def _model_assignments(values: list[str] | None) -> dict[str, str] | None:
    if values is None:
        return None
    assignments: dict[str, str] = {}
    for group in values:
        for assignment in group.split(","):
            try:
                axis, identifier = assignment.split("=", maxsplit=1)
            except ValueError as exc:
                raise VoicekitError(
                    "VK-CLI-010",
                    detail=f"invalid --models assignment {assignment!r}.",
                ) from exc
            axis = axis.strip().casefold()
            identifier = identifier.strip()
            if axis in assignments or axis not in {"stt", "llm", "tts"} or not identifier:
                raise VoicekitError(
                    "VK-CLI-010",
                    detail=f"invalid or duplicate --models axis {axis!r}.",
                )
            assignments[axis] = identifier
    return assignments


def _csv_tuple(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    result = tuple(item.strip() for item in value.split(",") if item.strip())
    return result or None


def _rows_or_table(
    rows: list[dict[str, object]],
    *,
    columns: tuple[str, ...],
    json_output: bool,
    next_command: str,
) -> None:
    if json_output:
        _json({"items": rows, "next_step": next_command})
        return
    table = Table(show_header=True)
    for column in columns:
        table.add_column(column.replace("_", " ").title())
    for row in rows:
        table.add_row(*(json.dumps(row.get(column), default=str) for column in columns))
    console.print(table)
    console.print(f"Next: {next_command}")


def _json(value: Mapping[str, object]) -> None:
    typer.echo(json.dumps(value, sort_keys=True, default=str))


def _guard(
    operation: Callable[[], ReturnT],
    *,
    json_output: bool = False,
) -> ReturnT:
    try:
        return operation()
    except typer.Exit:
        raise
    except VoicekitError as exc:
        _fail(exc, json_output=json_output)
    except Exception as exc:
        safe = scrub_secrets(str(exc))
        issue = "https://github.com/voicekit/voicekit/issues/new?title=" + quote(
            f"Unmapped CLI error: {type(exc).__name__}"
        )
        _fail(
            VoicekitError(
                "VK-CLI-009",
                detail=f"{type(exc).__name__}: {safe}. Report: {issue}",
            ),
            json_output=json_output,
        )


def _guard_async(
    operation: Callable[[], Coroutine[Any, Any, ReturnT]],
    *,
    json_output: bool = False,
) -> ReturnT:
    return _guard(lambda: asyncio.run(operation()), json_output=json_output)


def _fail(error: VoicekitError, *, json_output: bool = False) -> NoReturn:
    definition = ERROR_CATALOG[error.code]
    docs_url = error_docs_url(error.code)
    if json_output:
        _json(
            {
                "error": {
                    "code": error.code,
                    "cause": definition.cause,
                    "detail": error.detail,
                    "fix": definition.fix,
                    "docs": docs_url,
                },
                "next_step": definition.fix,
            }
        )
    else:
        stderr.print(f"[red]{error.code}[/red] {definition.cause}")
        if error.detail:
            stderr.print(str(scrub_secrets(error.detail)))
        stderr.print(f"Fix: {definition.fix}")
        stderr.print(f"Docs: {docs_url}")
        stderr.print(f"Next: {definition.fix}")
    raise typer.Exit(code=1)
