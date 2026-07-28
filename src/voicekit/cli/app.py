"""Production command rail for voicekit projects."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
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
    load_project_agent,
    next_step,
    require_manifest,
)
from voicekit.cli.doctor import Doctor, DoctorCheck
from voicekit.cli.environment import EnvFileStore, ensure_env_ignored
from voicekit.cli.keys import (
    LIVEKIT_ENV_VARS,
    LiveKitKeyValidator,
    ProviderKeyValidator,
    mask_value,
    required_entries,
)
from voicekit.cli.prompts import PromptChoice, QuestionaryPromptIO
from voicekit.cli.wizard import InitOptions, InitWizard
from voicekit.config.catalog import DEFAULT_PROVIDER_CATALOG, ProviderCatalogEntry
from voicekit.config.manifest import ManifestStore, RecipeSelection
from voicekit.deploy import (
    DockerDeploymentGenerator,
    DockerSmokeVerifier,
    FlyDeploymentManager,
    FlyPlan,
    LiveKitCloudDeploymentManager,
    LiveKitCloudPlan,
    PipecatCloudDeploymentManager,
    PipecatCloudPlan,
)
from voicekit.errors import ERROR_CATALOG, VoicekitError, error_docs_url
from voicekit.obs.logging import scrub_secrets
from voicekit.recipes.registry import DEFAULT_RECIPE_REGISTRY
from voicekit.recipes.source import install_recipe
from voicekit.relay.auth import RelayCredential
from voicekit.relay.client import RelayClient
from voicekit.relay.cloud_answer import (
    PipecatCloudProvider,
    pipecat_cloud_answer_path,
    pipecat_cloud_websocket_url,
)
from voicekit.storage.sqlite import SQLiteRepository
from voicekit.telephony.models import PipecatTarget, RollbackToken
from voicekit.testing.reporting import result_json, write_junit
from voicekit.testing.runner import run_project_tests
from voicekit.tunnel import TunnelPreference

if TYPE_CHECKING:
    from voicekit.telephony.plivo import PlivoAdapter
    from voicekit.telephony.telnyx import TelnyxAdapter
    from voicekit.telephony.twilio import TwilioAdapter
    from voicekit.telephony.vobiz import VobizAdapter

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
            open_browser=not no_open,
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
    """Place one paid test call through the configured carrier intent ledger."""

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
        adapter = _carrier(context)
        agent = load_project_agent(context)
        try:
            call_sid = adapter.start_call(
                manifest.phone_number,
                e164,
                _carrier_target(context, target_url),
                amd=True,
                record=bool(agent.phone and agent.phone.record),
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
        if manifest.runtime == "livekit":
            check = await LiveKitKeyValidator().validate(context.environment)
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
        manifest = require_manifest(context)
        prompt = QuestionaryPromptIO(interactive=sys.stdin.isatty() and not yes)
        values = dict(context.environment)
        pasted: dict[str, str] = {}
        if provider == "livekit":
            if manifest.runtime != "livekit":
                raise VoicekitError(
                    "VK-CLI-005",
                    detail="livekit project credentials apply only to a LiveKit-runtime project.",
                )
            if prompt.interactive:
                pasted = {
                    "LIVEKIT_URL": prompt.text("Paste LIVEKIT_URL:"),
                    "LIVEKIT_API_KEY": prompt.secret("Paste LIVEKIT_API_KEY:"),
                    "LIVEKIT_API_SECRET": prompt.secret("Paste LIVEKIT_API_SECRET:"),
                }
                if any(not value for value in pasted.values()):
                    raise VoicekitError(
                        "VK-CLI-004",
                        detail=f"{', '.join(LIVEKIT_ENV_VARS)} cannot be blank.",
                    )
                values.update(pasted)
            check = await LiveKitKeyValidator().validate(values)
        else:
            entry = _provider_entry(context, provider)
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
            next_command="voicekit init --recipe appointment-booking",
        )

    _guard(operation, json_output=json_output)


@recipes_app.command("add")
def recipes_add(
    name: Annotated[str, typer.Argument(help="Recipe id.")],
) -> None:
    """Copy the runtime-matching native recipe without overwriting project code."""

    def operation() -> None:
        context = _context()
        manifest = require_manifest(context)
        recipe = DEFAULT_RECIPE_REGISTRY.require(name, manifest.runtime)
        written = install_recipe(context.root, name=name, runtime=manifest.runtime)
        updated = manifest.model_copy(
            update={"recipe": RecipeSelection(name=name, version=recipe.version)}
        )
        try:
            ManifestStore(context.root / "voicekit.jsonc").save(updated)
        except Exception:
            for path in reversed(written):
                path.unlink(missing_ok=True)
            raise
        console.print(f"Added {name}@{recipe.version} ({len(written)} files).")
        console.print("Next: voicekit doctor")

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
        adapter = _carrier(_context())
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
        context = _context()
        carriers = require_manifest(context).carriers
        provider = carriers[0] if carriers else "carrier"
        _confirm(
            f"Buy one {provider} voice number in {country.upper()}/{area or '*'}?",
            yes=yes,
        )
        adapter = _carrier(context)
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
        adapter = _carrier(_context())
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
        adapter = _carrier(context, expected_public_base=target_url)
        try:
            token = adapter.point_inbound(
                selected_number,
                _carrier_target(context, target_url),
            )
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
        context = _context()
        provider = require_manifest(context).carriers[0]
        adapter = _carrier(context)
        try:
            adapter.restore(RollbackToken(provider=provider, token=token))
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
    """Run shared scenarios through the project's native runtime evaluator."""
    normalized_report = report.casefold() if report else None
    if normalized_report not in {None, "junit", "json"}:
        _fail(
            VoicekitError(
                "VK-CLI-010",
                detail="--report must be `junit` or `json`.",
            ),
            json_output=normalized_report == "json",
        )

    async def operation() -> None:
        context = _context()
        suite = await run_project_tests(
            context.root,
            filter_text=filter_text,
            audio=audio,
            live=live,
            environment=context.environment,
        )
        next_command = (
            "voicekit dev"
            if suite.passed
            else _test_retry_command(
                filter_text=filter_text,
                audio=audio,
                live=live,
            )
        )
        if normalized_report == "json":
            typer.echo(result_json(suite, next_step=next_command))
        else:
            table = Table(show_header=True)
            for column in ("Scenario", "Runtime", "Tier", "Status", "Stability", "Duration"):
                table.add_column(column)
            for case in suite.cases:
                table.add_row(
                    case.name,
                    case.runtime,
                    case.tier,
                    "PASS" if case.passed else "FAIL",
                    f"{case.stability:.1f}% ({len(case.attempts)} attempt"
                    f"{'s' if len(case.attempts) != 1 else ''})",
                    f"{case.duration_ms} ms",
                )
                if not case.passed:
                    for index, attempt in enumerate(case.attempts):
                        for failure in attempt.failures:
                            console.print(
                                f"[red]  {case.name} attempt {index + 1}:[/red] {failure}"
                            )
            console.print(table)
            if normalized_report == "junit":
                junit = write_junit(
                    suite,
                    context.root / ".voicekit" / "test-results.xml",
                )
                console.print(f"JUnit: {junit}")
            console.print(f"Next: {next_command}")
        if not suite.passed:
            raise typer.Exit(code=1)

    _guard_async(operation, json_output=normalized_report == "json")


@deploy_app.command("docker")
def deploy_command(
    yes: Annotated[bool, typer.Option("--yes", help="Confirm external mutations.")] = False,
    skip_smoke: Annotated[
        bool,
        typer.Option("--skip-smoke", help="Generate without running endpoint/call smoke."),
    ] = False,
    smoke_url: Annotated[
        str | None,
        typer.Option("--smoke", help="HTTPS deployment base to health-check and call."),
    ] = None,
    to_number: Annotated[
        str | None,
        typer.Option("--to", help="Paid smoke-call destination in E.164 form."),
    ] = None,
    engine_wheel: Annotated[
        Path | None,
        typer.Option("--engine-wheel", help="Local unpublished voicekit wheel."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable artifact and smoke facts."),
    ] = False,
) -> None:
    """Generate, validate, and optionally smoke the canonical Docker target."""

    async def operation() -> None:
        DEFAULT_CAPABILITIES.require("deploy", "docker")
        if skip_smoke and smoke_url is not None:
            raise VoicekitError(
                "VK-CLI-010",
                detail="--skip-smoke and --smoke cannot be used together.",
            )
        if to_number is not None and smoke_url is None:
            raise VoicekitError("VK-CLI-010", detail="--to requires --smoke URL.")
        context = _context()
        manifest = require_manifest(context)
        generator = DockerDeploymentGenerator(context.root)
        artifacts = await asyncio.to_thread(
            generator.generate,
            engine_wheel=engine_wheel,
        )
        await asyncio.to_thread(generator.validate, artifacts)
        updated = manifest.model_copy(update={"deploy_target": "docker"})
        await asyncio.to_thread(
            ManifestStore(context.root / "voicekit.jsonc").save,
            updated,
        )

        smoke: dict[str, object] | None = None
        call_id: str | None = None
        if smoke_url is not None:
            smoke_result = await DockerSmokeVerifier().verify(smoke_url)
            smoke = asdict(smoke_result)
            if "phone" not in manifest.channels:
                raise VoicekitError(
                    "VK-DEP-004",
                    detail=(
                        "this web-only project requires a manual browser conversation; "
                        "omit --smoke and follow the printed runbook."
                    ),
                )
            destination = to_number or context.environment.get("VOICEKIT_SMOKE_TO")
            if not destination:
                raise VoicekitError(
                    "VK-DEP-004",
                    detail="a phone smoke requires --to E164 or VOICEKIT_SMOKE_TO.",
                )
            _confirm(
                (
                    f"Place one paid smoke call from {manifest.phone_number} "
                    f"to {destination} through {smoke_result.url}?"
                ),
                yes=yes,
            )
            adapter = _carrier(context, expected_public_base=smoke_result.url)
            agent = load_project_agent(context)
            try:
                call_id = await asyncio.to_thread(
                    adapter.start_call,
                    cast("str", manifest.phone_number),
                    destination,
                    _carrier_target(context, smoke_result.url),
                    amd=True,
                    record=bool(agent.phone and agent.phone.record),
                )
            finally:
                adapter.ledger.close()

        artifact_rows = {
            "dockerfile": str(artifacts.dockerfile),
            "compose": str(artifacts.compose),
            "dockerignore": str(artifacts.dockerignore),
            "environment_example": str(artifacts.environment_example),
            "engine_wheel": (
                None if artifacts.engine_wheel is None else str(artifacts.engine_wheel)
            ),
        }
        next_command = "docker compose -f compose.voicekit.yaml up -d --build"
        if json_output:
            _json(
                {
                    "target": "docker",
                    "artifacts": artifact_rows,
                    "smoke": smoke,
                    "call_id": call_id,
                    "next_step": next_command,
                }
            )
            return
        console.print("Docker deployment artifacts are valid.")
        for name, path in artifact_rows.items():
            if path is not None:
                console.print(f"{name}: {path}")
        console.print(f"Next: {next_command}")
        if manifest.phone_number is not None:
            console.print(
                "After HTTPS ingress is ready: "
                f"voicekit numbers point {manifest.phone_number} --url "
                "https://voice.example.com --yes"
            )
            if smoke_url is None and not skip_smoke:
                wheel_option = (
                    ""
                    if artifacts.engine_wheel is None
                    else f" --engine-wheel {shlex.quote(str(artifacts.engine_wheel))}"
                )
                console.print(
                    "Then: voicekit deploy docker --smoke https://voice.example.com "
                    f"--to +15551234567{wheel_option} --yes"
                )
        elif smoke_url is None and not skip_smoke:
            console.print("Then complete one browser conversation through your allowed web origin.")

    _guard_async(operation, json_output=json_output)


@deploy_app.command("fly")
def deploy_fly_command(
    app_name: Annotated[
        str,
        typer.Option("--app", help="Exact Fly application name for the results companion."),
    ],
    organization: Annotated[
        str,
        typer.Option("--org", help="Exact Fly organization slug."),
    ],
    region: Annotated[
        str,
        typer.Option("--region", help="Exact Fly/Managed Postgres region."),
    ],
    postgres_name: Annotated[
        str,
        typer.Option("--postgres-name", help="Existing or new Managed Postgres name."),
    ],
    bucket_name: Annotated[
        str,
        typer.Option("--bucket", help="Existing or new private Tigris bucket name."),
    ],
    postgres_plan: Annotated[
        str,
        typer.Option("--postgres-plan", help="Fly Managed Postgres plan."),
    ],
    postgres_volume_gb: Annotated[
        int,
        typer.Option("--postgres-volume-gb", help="Managed Postgres storage (10-500 GB)."),
    ],
    adopt: Annotated[
        bool,
        typer.Option("--adopt", help="Adopt exact pre-existing resources after verification."),
    ] = False,
    rotate_credentials: Annotated[
        bool,
        typer.Option(
            "--rotate-credentials",
            help="Rotate relay/results credentials while retaining the prior pair.",
        ),
    ] = False,
    rollback_created: Annotated[
        bool,
        typer.Option(
            "--rollback-created",
            help="Permanently destroy only resources ledgered as created by voicekit.",
        ),
    ] = False,
    skip_smoke: Annotated[
        bool,
        typer.Option("--skip-smoke", help="Deploy without platform and signed relay smoke."),
    ] = False,
    engine_wheel: Annotated[
        Path | None,
        typer.Option("--engine-wheel", help="Local unpublished voicekit wheel."),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Confirm paid/destructive mutations.")] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable resource and smoke facts."),
    ] = False,
) -> None:
    """Provision, resume, rotate, validate, or roll back the Fly companion."""

    async def operation() -> None:
        DEFAULT_CAPABILITIES.require("deploy", "fly")
        if rollback_created and (
            adopt or rotate_credentials or skip_smoke or engine_wheel is not None
        ):
            raise VoicekitError(
                "VK-CLI-010",
                detail=(
                    "--rollback-created cannot be combined with adoption, rotation, "
                    "smoke, or wheel options."
                ),
            )
        context = _context()
        manifest = require_manifest(context)
        selected_carriers = tuple(
            provider
            for provider in manifest.carriers
            if provider in {"twilio", "telnyx", "vobiz", "plivo"}
        )
        plan = FlyPlan(
            app_name=app_name,
            organization=organization,
            region=region,
            postgres_name=postgres_name,
            bucket_name=bucket_name,
            callback_providers=selected_carriers,
            postgres_plan=postgres_plan,
            postgres_volume_gb=postgres_volume_gb,
        )
        manager = FlyDeploymentManager(context.root)
        manifest_store = ManifestStore(context.root / "voicekit.jsonc")
        if rollback_created:
            _confirm(
                (
                    "Permanently destroy only Fly resources marked created-by-voicekit "
                    f"for {app_name}? Managed data cannot be recovered."
                ),
                yes=yes,
            )
            state = await asyncio.to_thread(manager.rollback_created, plan)
            manifest_store.save(manifest.model_copy(update={"deploy_target": None}))
            next_command = (
                f"voicekit deploy fly --app {app_name} --org {organization} "
                f"--region {region} --postgres-name {postgres_name} "
                f"--bucket {bucket_name} --postgres-plan {postgres_plan} "
                f"--postgres-volume-gb {postgres_volume_gb} --yes"
            )
            payload = {
                "target": "fly",
                "rolled_back": True,
                "resources": asdict(state),
                "next_step": next_command,
            }
            if json_output:
                _json(payload)
            else:
                console.print("Fly resources created by voicekit were rolled back.")
                console.print(f"Next: {next_command}")
            return

        _confirm(
            (
                f"Provision or reuse Fly app {app_name}, Managed Postgres "
                f"{plan.postgres_name}, and private Tigris bucket {plan.bucket_name}? "
                "These resources can incur charges."
            ),
            yes=yes,
        )
        report = await manager.deploy(
            plan,
            environment=context.environment,
            engine_wheel=engine_wheel,
            adopt=adopt,
            rotate_credentials=rotate_credentials,
            skip_smoke=skip_smoke,
        )
        manifest_store.save(manifest.model_copy(update={"deploy_target": "fly"}))
        next_target = "pipecat-cloud" if manifest.runtime == "pipecat" else "livekit-cloud"
        next_command = f"voicekit deploy {next_target} --relay-url {plan.public_base} --yes"
        artifact_rows = {
            "dockerfile": str(report.artifacts.dockerfile),
            "config": str(report.artifacts.config),
            "dockerignore": str(report.artifacts.dockerignore),
            "engine_wheel": (
                None
                if report.artifacts.engine_wheel is None
                else str(report.artifacts.engine_wheel)
            ),
        }
        payload = {
            "target": "fly",
            "resources": asdict(report.state),
            "artifacts": artifact_rows,
            "smoke": None if report.smoke is None else asdict(report.smoke),
            "next_step": next_command,
        }
        if json_output:
            _json(payload)
            return
        console.print("Fly results companion deployment completed.")
        console.print(f"URL: {plan.public_base}")
        console.print(
            "Signed readiness: " + ("green" if report.smoke is not None else "skipped by operator")
        )
        console.print(f"Resource ledger: {manager.store.path}")
        console.print(f"Next: {next_command}")

    _guard_async(operation, json_output=json_output)


@deploy_app.command("pipecat-cloud")
def deploy_pipecat_cloud_command(
    agent_name: Annotated[
        str,
        typer.Option("--agent", help="Exact deployed Pipecat Cloud agent name."),
    ],
    organization: Annotated[
        str,
        typer.Option("--org", help="Exact Pipecat Cloud organization slug."),
    ],
    region: Annotated[
        str,
        typer.Option("--region", help="Exact Pipecat Cloud region."),
    ],
    secret_set: Annotated[
        str,
        typer.Option("--secret-set", help="Exact Pipecat Cloud secret-set name."),
    ],
    image: Annotated[
        str,
        typer.Option("--image", help="Pre-pushed immutable tagged worker image."),
    ],
    min_agents: Annotated[
        int,
        typer.Option("--min-agents", help="Minimum warm agents (0-50)."),
    ],
    max_agents: Annotated[
        int,
        typer.Option("--max-agents", help="Maximum agents (1-50)."),
    ],
    profile: Annotated[
        str,
        typer.Option("--profile", help="agent-1x, agent-2x, or agent-3x."),
    ],
    relay_url: Annotated[
        str,
        typer.Option("--relay-url", help="Validated user-owned results companion URL."),
    ],
    image_pull_secret: Annotated[
        str | None,
        typer.Option("--image-pull-secret", help="Private-registry credential name."),
    ] = None,
    prepare_only: Annotated[
        bool,
        typer.Option(
            "--prepare-only",
            help="Generate the build context without touching Pipecat Cloud.",
        ),
    ] = False,
    adopt: Annotated[
        bool,
        typer.Option("--adopt", help="Adopt an exact existing cloud agent."),
    ] = False,
    cutover: Annotated[
        bool,
        typer.Option(
            "--cutover/--no-cutover",
            help="Point the configured phone number after deployment.",
        ),
    ] = True,
    telnyx_texml_ready: Annotated[
        bool,
        typer.Option(
            "--telnyx-texml-ready",
            help="Confirm the selected TeXML app uses the printed hosted-answer URL.",
        ),
    ] = False,
    smoke_to: Annotated[
        str | None,
        typer.Option("--smoke-to", help="Paid phone-smoke destination in E.164 form."),
    ] = None,
    skip_smoke: Annotated[
        bool,
        typer.Option("--skip-smoke", help="Skip platform and phone smoke evidence."),
    ] = False,
    rollback_created: Annotated[
        bool,
        typer.Option(
            "--rollback-created",
            help="Restore cutover and delete only a voicekit-created cloud agent.",
        ),
    ] = False,
    engine_wheel: Annotated[
        Path | None,
        typer.Option("--engine-wheel", help="Local unpublished voicekit wheel."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm paid/live/destructive mutations."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable deployment facts."),
    ] = False,
) -> None:
    """Prepare, deploy, cut over, smoke, resume, or roll back Pipecat Cloud."""

    async def operation() -> None:
        DEFAULT_CAPABILITIES.require("deploy", "pipecat-cloud")
        context = _context()
        manifest = require_manifest(context)
        if manifest.runtime != "pipecat":
            raise VoicekitError(
                "VK-DEP-008",
                detail="pipecat-cloud requires a Pipecat-runtime project.",
            )
        plan = PipecatCloudPlan(
            agent_name=agent_name,
            organization=organization,
            region=region,
            secret_set=secret_set,
            image=image,
            relay_url=relay_url,
            min_agents=min_agents,
            max_agents=max_agents,
            profile=cast(Any, profile),
            image_pull_secret=image_pull_secret,
        )
        manager = PipecatCloudDeploymentManager(context.root)
        manifest_store = ManifestStore(context.root / "voicekit.jsonc")
        if prepare_only:
            if adopt or rollback_created or smoke_to is not None or skip_smoke:
                raise VoicekitError(
                    "VK-CLI-010",
                    detail=(
                        "--prepare-only cannot be combined with adoption, rollback, "
                        "or smoke options."
                    ),
                )
            artifacts = await asyncio.to_thread(
                manager.prepare,
                plan,
                engine_wheel=engine_wheel,
            )
            build = (
                f"docker build -t {shlex.quote(plan.image)} {shlex.quote(str(artifacts.context))}"
            )
            push = f"docker push {shlex.quote(plan.image)}"
            payload = {
                "target": "pipecat-cloud",
                "prepared": True,
                "context": str(artifacts.context),
                "dockerfile": str(artifacts.dockerfile),
                "digest": artifacts.digest,
                "next_step": f"{build} && {push}",
            }
            if json_output:
                _json(payload)
            else:
                console.print("Secret-free Pipecat Cloud build context is ready.")
                console.print(f"Context: {artifacts.context}")
                console.print(f"Next: {payload['next_step']}")
            return

        if smoke_to is not None and skip_smoke:
            raise VoicekitError("VK-CLI-010", detail="--smoke-to conflicts with --skip-smoke.")
        phone_provider = manifest.carriers[0] if manifest.carriers else None
        target = (
            None
            if phone_provider is None
            else _pipecat_cloud_target(
                plan,
                cast("PipecatCloudProvider", phone_provider),
            )
        )
        if (
            phone_provider == "telnyx"
            and not rollback_created
            and (cutover or smoke_to is not None)
            and not telnyx_texml_ready
        ):
            raise VoicekitError(
                "VK-DEP-008",
                detail=(
                    "configure the selected Telnyx TeXML application URL as "
                    f"{cast('PipecatTarget', target).answer_url}, then pass "
                    "--telnyx-texml-ready."
                ),
            )
        if (
            phone_provider is not None
            and not rollback_created
            and not skip_smoke
            and smoke_to is None
        ):
            raise VoicekitError(
                "VK-DEP-004",
                detail="phone deployment smoke requires --smoke-to E164 or --skip-smoke.",
            )
        if rollback_created:
            _confirm(
                (
                    f"Restore any ledgered carrier cutover and delete only the "
                    f"voicekit-created Pipecat Cloud agent {agent_name}?"
                ),
                yes=yes,
            )
            state = manager.store.load()
            if state is not None and state.cutover_token is not None:
                adapter = _carrier(context)
                try:
                    adapter.restore(
                        RollbackToken(
                            provider=cast(str, state.cutover_provider),
                            token=state.cutover_token,
                        )
                    )
                finally:
                    adapter.ledger.close()
                state = state.checkpoint(
                    cutover_provider=None,
                    cutover_token=None,
                )
                manager.store.save(state)
            state = await asyncio.to_thread(manager.rollback_created, plan)
            manifest_store.save(manifest.model_copy(update={"deploy_target": None}))
            payload = {
                "target": "pipecat-cloud",
                "rolled_back": True,
                "resources": asdict(state),
                "next_step": "voicekit deploy pipecat-cloud --prepare-only ...",
            }
            if json_output:
                _json(payload)
            else:
                console.print("Pipecat Cloud resources created by voicekit were rolled back.")
                console.print(f"Next: {payload['next_step']}")
            return

        _confirm(
            (
                f"Deploy {agent_name} to Pipecat Cloud in {region}"
                + (
                    f", point {manifest.phone_number}, and place one paid smoke call"
                    if phone_provider is not None and cutover and not skip_smoke
                    else ""
                )
                + "? This can incur charges."
            ),
            yes=yes,
        )
        report = await manager.deploy(
            plan,
            environment=context.environment,
            engine_wheel=engine_wheel,
            adopt=adopt,
            skip_session_smoke=skip_smoke,
        )
        state = report.state
        adapter = None
        new_cutover: RollbackToken | None = None
        active_cutover = (
            None
            if state.cutover_token is None
            else RollbackToken(
                provider=cast(str, state.cutover_provider),
                token=state.cutover_token,
            )
        )
        try:
            if (
                target is not None
                and cutover
                and manifest.phone_number is not None
                and state.cutover_token is None
            ):
                adapter = _carrier(context, expected_public_base=plan.relay_url)
                new_cutover = await asyncio.to_thread(
                    adapter.point_inbound,
                    manifest.phone_number,
                    target,
                )
                state = state.checkpoint(
                    cutover_provider=new_cutover.provider,
                    cutover_token=new_cutover.token,
                )
                active_cutover = new_cutover
                manager.store.save(state)
            if target is not None and smoke_to is not None:
                adapter = adapter or _carrier(
                    context,
                    expected_public_base=plan.relay_url,
                )
                agent = load_project_agent(context)
                call_id = await asyncio.to_thread(
                    adapter.start_call,
                    cast(str, manifest.phone_number),
                    smoke_to,
                    target,
                    amd=True,
                    record=bool(agent.phone and agent.phone.record),
                )
                await _verify_cloud_phone_smoke(
                    plan.relay_url,
                    context.environment,
                    call_id,
                )
                state = state.checkpoint(smoke_call_id=call_id)
                manager.store.save(state)
        except Exception:
            if adapter is not None and active_cutover is not None:
                await asyncio.to_thread(adapter.restore, active_cutover)
                state = state.checkpoint(
                    cutover_provider=None,
                    cutover_token=None,
                )
                manager.store.save(state)
            raise
        finally:
            if adapter is not None:
                adapter.ledger.close()
        manifest_store.save(manifest.model_copy(update={"deploy_target": "pipecat-cloud"}))
        payload = {
            "target": "pipecat-cloud",
            "resources": asdict(state),
            "artifacts": {
                "context": str(report.artifacts.context),
                "dockerfile": str(report.artifacts.dockerfile),
                "config": str(report.artifacts.platform_config),
                "digest": report.artifacts.digest,
            },
            "smoke": asdict(report.smoke),
            "answer_url": None if target is None else target.answer_url,
            "next_step": "voicekit calls list",
        }
        if json_output:
            _json(payload)
            return
        console.print("Pipecat Cloud deployment completed.")
        console.print(f"Resource ledger: {manager.store.path}")
        if target is not None:
            console.print(f"Hosted carrier answer: {target.answer_url}")
        console.print("Next: voicekit calls list")

    _guard_async(operation, json_output=json_output)


@deploy_app.command("livekit-cloud")
def deploy_livekit_cloud_command(
    agent_name: Annotated[
        str,
        typer.Option("--agent", help="Exact registered LiveKit agent name."),
    ],
    project: Annotated[
        str,
        typer.Option("--project", help="Exact authenticated LiveKit Cloud project."),
    ],
    region: Annotated[
        str,
        typer.Option("--region", help="Exact LiveKit Cloud region."),
    ],
    relay_url: Annotated[
        str,
        typer.Option("--relay-url", help="Validated user-owned results companion URL."),
    ],
    agent_id: Annotated[
        str | None,
        typer.Option("--agent-id", help="Exact existing agent id for adoption."),
    ] = None,
    adopt: Annotated[
        bool,
        typer.Option("--adopt", help="Adopt the exact existing agent id."),
    ] = False,
    smoke_to: Annotated[
        str | None,
        typer.Option("--smoke-to", help="Paid LiveKit SIP smoke destination."),
    ] = None,
    skip_smoke: Annotated[
        bool,
        typer.Option("--skip-smoke", help="Skip room/PSTN and relay smoke evidence."),
    ] = False,
    rollback: Annotated[
        bool,
        typer.Option("--rollback", help="Roll back the ledgered version or created agent."),
    ] = False,
    engine_wheel: Annotated[
        Path | None,
        typer.Option("--engine-wheel", help="Local unpublished voicekit wheel."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm paid/live/destructive mutations."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable deployment facts."),
    ] = False,
) -> None:
    """Deploy, smoke, resume, adopt, or roll back a LiveKit Cloud agent."""

    async def operation() -> None:
        DEFAULT_CAPABILITIES.require("deploy", "livekit-cloud")
        context = _context()
        manifest = require_manifest(context)
        if manifest.runtime != "livekit":
            raise VoicekitError(
                "VK-DEP-008",
                detail="livekit-cloud requires a LiveKit-runtime project.",
            )
        if smoke_to is not None and skip_smoke:
            raise VoicekitError("VK-CLI-010", detail="--smoke-to conflicts with --skip-smoke.")
        if "phone" in manifest.channels and not rollback and not skip_smoke and smoke_to is None:
            raise VoicekitError(
                "VK-DEP-004",
                detail="phone deployment smoke requires --smoke-to E164 or --skip-smoke.",
            )
        plan = LiveKitCloudPlan(
            agent_name=agent_name,
            project=project,
            region=region,
            relay_url=relay_url,
            agent_id=agent_id,
        )
        manager = LiveKitCloudDeploymentManager(context.root)
        manifest_store = ManifestStore(context.root / "voicekit.jsonc")
        if rollback:
            if adopt or skip_smoke or smoke_to is not None or engine_wheel is not None:
                raise VoicekitError(
                    "VK-CLI-010",
                    detail="--rollback cannot be combined with deploy or smoke options.",
                )
            _confirm(
                f"Roll back the ledgered LiveKit Cloud agent {agent_name}?",
                yes=yes,
            )
            state = await asyncio.to_thread(manager.rollback, plan)
            manifest_store.save(manifest.model_copy(update={"deploy_target": None}))
            payload = {
                "target": "livekit-cloud",
                "rolled_back": True,
                "resources": asdict(state),
                "next_step": "voicekit deploy livekit-cloud --help",
            }
            if json_output:
                _json(payload)
            else:
                console.print("LiveKit Cloud rollback completed.")
                console.print(f"Next: {payload['next_step']}")
            return
        _confirm(
            (
                f"Deploy {agent_name} to LiveKit Cloud project {project} in {region}"
                + (" and place one paid SIP smoke call" if smoke_to is not None else "")
                + "? This can incur charges."
            ),
            yes=yes,
        )
        report = await manager.deploy(
            plan,
            environment=context.environment,
            engine_wheel=engine_wheel,
            adopt=adopt,
            skip_session_smoke=skip_smoke,
            smoke_to=smoke_to,
        )
        manifest_store.save(manifest.model_copy(update={"deploy_target": "livekit-cloud"}))
        payload = {
            "target": "livekit-cloud",
            "resources": asdict(report.state),
            "artifacts": {
                "context": str(report.artifacts.context),
                "dockerfile": str(report.artifacts.dockerfile),
                "digest": report.artifacts.digest,
            },
            "smoke": asdict(report.smoke),
            "next_step": "voicekit calls list",
        }
        if json_output:
            _json(payload)
            return
        console.print("LiveKit Cloud deployment completed.")
        console.print(f"Resource ledger: {manager.store.path}")
        console.print("Next: voicekit calls list")

    _guard_async(operation, json_output=json_output)


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


def _carrier(
    context: ProjectContext,
    *,
    expected_public_base: str | None = None,
) -> TwilioAdapter | TelnyxAdapter | VobizAdapter | PlivoAdapter:
    from voicekit.telephony.telnyx import TelnyxAdapter

    manifest = require_manifest(context)
    if manifest.carriers in ([], ["twilio"]):
        return _twilio(context, expected_public_base=expected_public_base)
    if manifest.carriers == ["telnyx"]:
        return TelnyxAdapter(
            api_key=context.environment.get("TELNYX_API_KEY"),
            public_key=context.environment.get("TELNYX_PUBLIC_KEY"),
            connection_id=context.environment.get("TELNYX_CONNECTION_ID"),
            ledger_path=context.root / ".voicekit" / "telephony.sqlite3",
        )
    if manifest.carriers == ["vobiz"]:
        from voicekit.telephony.vobiz import VobizAdapter

        return VobizAdapter(
            auth_id=context.environment.get("VOBIZ_AUTH_ID"),
            auth_token=context.environment.get("VOBIZ_AUTH_TOKEN"),
            ledger_path=context.root / ".voicekit" / "telephony.sqlite3",
            expected_public_base=expected_public_base,
        )
    if manifest.carriers == ["plivo"]:
        from voicekit.telephony.plivo import PlivoAdapter

        return PlivoAdapter(
            auth_id=context.environment.get("PLIVO_AUTH_ID"),
            auth_token=context.environment.get("PLIVO_AUTH_TOKEN"),
            ledger_path=context.root / ".voicekit" / "telephony.sqlite3",
            expected_public_base=expected_public_base,
        )
    raise VoicekitError(
        "VK-CLI-005",
        detail=(
            "this command requires Twilio, Telnyx, Vobiz, or Plivo; "
            "generic SIP routing is operator-managed."
        ),
    )


def _twilio(
    context: ProjectContext,
    *,
    expected_public_base: str | None = None,
) -> TwilioAdapter:
    """Compatibility seam for Twilio-specific tests and third-party CLI wrappers."""
    from voicekit.telephony.twilio import TwilioAdapter

    return TwilioAdapter(
        account_sid=context.environment.get("TWILIO_ACCOUNT_SID"),
        auth_token=context.environment.get("TWILIO_AUTH_TOKEN"),
        ledger_path=context.root / ".voicekit" / "telephony.sqlite3",
        expected_public_base=expected_public_base,
    )


def _carrier_target(context: ProjectContext, public_base: str) -> PipecatTarget:
    manifest = require_manifest(context)
    if manifest.carriers == ["telnyx"]:
        return PipecatTarget(
            public_base,
            ws_path="/telnyx/media",
            answer_path="/telnyx/answer",
            event_path="/telnyx/events",
            recording_path="/telnyx/recordings",
            amd_path="/telnyx/amd",
        )
    if manifest.carriers == ["vobiz"]:
        return PipecatTarget(
            public_base,
            ws_path="/vobiz/media",
            answer_path="/vobiz/answer",
            event_path="/vobiz/events",
            recording_path="/vobiz/recordings",
            amd_path="/vobiz/amd",
        )
    if manifest.carriers == ["plivo"]:
        return PipecatTarget(
            public_base,
            ws_path="/plivo/media",
            answer_path="/plivo/answer",
            event_path="/plivo/events",
            recording_path="/plivo/recordings",
            amd_path="/plivo/amd",
        )
    if manifest.carriers != ["twilio"]:
        raise VoicekitError(
            "VK-CLI-005",
            detail=(
                "this command requires Twilio, Telnyx, Vobiz, or Plivo; "
                "generic SIP has no Pipecat target."
            ),
        )
    return PipecatTarget(public_base)


def _pipecat_cloud_target(
    plan: PipecatCloudPlan,
    provider: PipecatCloudProvider,
) -> PipecatTarget:
    answer_path = pipecat_cloud_answer_path(
        region=plan.region,
        organization=plan.organization,
        agent_name=plan.agent_name,
        provider=provider,
    )
    return PipecatTarget(
        plan.relay_url,
        ws_path="/v1/pipecat-cloud/unused",
        answer_path=answer_path,
        event_path=f"/{provider}/events",
        recording_path=f"/{provider}/recordings",
        amd_path=f"/{provider}/amd",
        stream_url_override=pipecat_cloud_websocket_url(
            region=plan.region,
            organization=plan.organization,
            agent_name=plan.agent_name,
            provider=provider,
        ),
    )


async def _verify_cloud_phone_smoke(
    relay_url: str,
    environment: Mapping[str, str],
    call_id: str,
    *,
    timeout_s: float = 900,
) -> None:
    relay = RelayCredential.parse(environment.get("VOICEKIT_RELAY_CREDENTIAL", ""))
    deadline = asyncio.get_running_loop().time() + timeout_s
    async with RelayClient(relay_url, relay) as client:
        while True:
            try:
                call = await client.get_call(call_id)
            except VoicekitError as exc:
                if exc.code != "VK-OBS-003":
                    raise
                call = None
            if call is not None and call.ended_at is not None:
                if call.webhook_status == "delivered":
                    return
                if call.webhook_status == "dead_lettered":
                    raise VoicekitError(
                        "VK-DEP-004",
                        detail=f"cloud smoke call {call_id!r} dead-lettered its result.",
                    )
            if asyncio.get_running_loop().time() >= deadline:
                raise VoicekitError(
                    "VK-DEP-004",
                    detail=(
                        f"cloud smoke call {call_id!r} did not terminalize and "
                        "deliver its result before timeout."
                    ),
                )
            await asyncio.sleep(2)


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


def _test_retry_command(
    *,
    filter_text: str | None,
    audio: bool,
    live: bool,
) -> str:
    arguments = ["voicekit", "test"]
    if filter_text:
        arguments.extend(("--filter", filter_text))
    if audio:
        arguments.append("--audio")
    if live:
        arguments.append("--live")
    return shlex.join(arguments)


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
