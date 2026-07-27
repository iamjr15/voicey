from __future__ import annotations

import json
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import voicekit.cli.context as cli_context
from voicekit.capabilities import (
    DEFAULT_CAPABILITIES,
    Capability,
    CapabilityRegistry,
)
from voicekit.cli.checkpoint import InitCheckpoint, InitCheckpointStore
from voicekit.cli.context import (
    ProjectContext,
    discover_project,
    load_project_agent,
    next_step,
    require_manifest,
)
from voicekit.cli.environment import EnvFileStore, ensure_env_ignored, merged_environment
from voicekit.cli.keys import (
    LiveKitKeyValidator,
    ProviderKeyValidator,
    mask_value,
    required_entries,
)
from voicekit.cli.scaffold import ScaffoldWriter, ScratchScaffold
from voicekit.config.catalog import ProviderCatalog, ProviderCatalogEntry
from voicekit.config.manifest import (
    ManifestState,
    ManifestStore,
    ProjectManifest,
    RecipeSelection,
)
from voicekit.config.models import Agent, ModelAxis, Models, Results, Web
from voicekit.errors import VoicekitError
from voicekit.recipes.registry import (
    DEFAULT_RECIPE_REGISTRY,
    RecipeDefinition,
    RecipeRegistry,
)


class FakeHttpClient:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.requests: list[tuple[str, dict[str, str], float]] = []

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> httpx.Response:
        self.requests.append((url, dict(headers), timeout_s))
        return httpx.Response(
            self.status_code,
            request=httpx.Request("GET", url),
        )


class RaisingHttpClient:
    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> httpx.Response:
        del url, headers, timeout_s
        raise httpx.ConnectError("offline")


def test_project_agent_loader_catalogs_import_shape_and_runtime_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = ProjectManifest(
        project_name="loader-test",
        runtime="pipecat",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"web"}),
        models={
            "stt": "deepgram/nova-3",
            "llm": "anthropic/claude-sonnet-5",
            "tts": "cartesia/sonic-3.5",
        },
    )
    context = ProjectContext(
        root=tmp_path,
        manifest=manifest,
        checkpoint=False,
        environment={},
    )
    sys.modules.pop(manifest.agent_module, None)

    def missing(_name: str) -> object:
        raise ImportError("missing")

    monkeypatch.setattr(cli_context.importlib, "import_module", missing)
    with pytest.raises(VoicekitError, match="must export an Agent"):
        load_project_agent(context)

    def broken(_name: str) -> object:
        raise RuntimeError("private detail")

    monkeypatch.setattr(cli_context.importlib, "import_module", broken)
    with pytest.raises(VoicekitError, match="failed to load \\(RuntimeError\\)"):
        load_project_agent(context)

    def wrong_type(_name: str) -> object:
        return SimpleNamespace(agent=object())

    monkeypatch.setattr(cli_context.importlib, "import_module", wrong_type)
    with pytest.raises(VoicekitError, match="is not a voicekit Agent"):
        load_project_agent(context)

    livekit_agent = Agent(
        name="loader-test",
        runtime="livekit",
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Test the project loader.",
        flow="flow:entry",
        tools="tools",
        web=Web(enabled=True, allowed_origins=["https://app.example.test"]),
        results=Results(
            webhook="https://receiver.example.test/results",
            secret_env="VOICEKIT_WEBHOOK_SECRET",  # pragma: allowlist secret
        ),
    )

    def wrong_runtime(_name: str) -> object:
        return SimpleNamespace(agent=livekit_agent)

    monkeypatch.setattr(cli_context.importlib, "import_module", wrong_runtime)
    with pytest.raises(VoicekitError, match="different runtimes"):
        load_project_agent(context)


def test_capabilities_and_recipes_report_runtime_and_recipe_availability() -> None:
    assert DEFAULT_CAPABILITIES.require("runtime", "pipecat").enabled
    assert DEFAULT_CAPABILITIES.require("runtime", "livekit").enabled

    available = DEFAULT_RECIPE_REGISTRY.list(include_unavailable=False)
    assert [recipe.name for recipe in available] == [
        "appointment-booking",
        "front-desk",
        "lead-intake",
        "restaurant-reservations",
    ]
    assert DEFAULT_RECIPE_REGISTRY.require("appointment-booking", "pipecat") == available[0]
    assert DEFAULT_RECIPE_REGISTRY.require("appointment-booking", "livekit") == available[0]
    assert available[0].runtimes == frozenset({"pipecat", "livekit"})


@pytest.mark.asyncio
async def test_livekit_key_validator_uses_read_only_room_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Room:
        async def list_rooms(self, _request: object) -> object:
            calls.append("list_rooms")
            return object()

    class FakeLiveKitAPI:
        room = Room()

        def __init__(self, *, url: str, api_key: str, api_secret: str) -> None:
            calls.extend((url, api_key, api_secret))

        async def aclose(self) -> None:
            calls.append("close")

    monkeypatch.setattr("livekit.api.LiveKitAPI", FakeLiveKitAPI)
    validator = LiveKitKeyValidator()

    missing = await validator.validate({})
    valid = await validator.validate(
        {
            "LIVEKIT_URL": "wss://project.livekit.cloud",
            "LIVEKIT_API_KEY": "key",  # pragma: allowlist secret
            "LIVEKIT_API_SECRET": "secret",  # pragma: allowlist secret
        }
    )

    assert missing.status == "missing"
    assert valid.status == "valid"
    assert calls == [
        "wss://project.livekit.cloud",
        "key",
        "secret",
        "list_rooms",
        "close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "expected"),
    [
        ("value", "invalid"),
        ("auth", "invalid"),
        ("network", "indeterminate"),
    ],
)
async def test_livekit_key_validator_classifies_safe_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    expected: str,
) -> None:
    class ProviderError(RuntimeError):
        status = 401

    class Room:
        async def list_rooms(self, _request: object) -> object:
            if failure_kind == "value":
                raise ValueError("bad settings")
            if failure_kind == "auth":
                raise ProviderError("provider rejection")
            raise RuntimeError("offline")

    class FailingLiveKitAPI:
        room = Room()

        def __init__(self, **_values: str) -> None:
            return

        async def aclose(self) -> None:
            return

    monkeypatch.setattr("livekit.api.LiveKitAPI", FailingLiveKitAPI)
    result = await LiveKitKeyValidator().validate(
        {
            "LIVEKIT_URL": "wss://project.livekit.cloud",
            "LIVEKIT_API_KEY": "key",  # pragma: allowlist secret
            "LIVEKIT_API_SECRET": "secret",  # pragma: allowlist secret
        }
    )

    assert result.status == expected


def test_capability_registry_indexes_filters_and_catalogs_bad_entries() -> None:
    registry = CapabilityRegistry(
        (
            Capability("runtime", "zeta", "Enabled.", True),
            Capability(
                "runtime",
                "alpha",
                "Disabled.",
                False,
                install_extra="future",
            ),
        )
    )

    assert [item.id for item in registry.choices("runtime")] == ["zeta"]
    assert [item.id for item in registry.choices("runtime", include_unavailable=True)] == [
        "alpha",
        "zeta",
    ]
    assert registry.get("runtime", "missing") is None
    with pytest.raises(VoicekitError, match="voicekit\\[future\\]"):
        registry.require("runtime", "alpha")
    with pytest.raises(VoicekitError, match="unknown"):
        registry.require("runtime", "missing")
    with pytest.raises(AssertionError, match="duplicate"):
        CapabilityRegistry(
            (
                Capability("runtime", "same", "First.", True),
                Capability("runtime", "same", "Second.", True),
            )
        )


def test_recipe_registry_handles_unknown_runtime_source_and_duplicates() -> None:
    capabilities = CapabilityRegistry((Capability("recipe", "scratch", "Scratch.", True),))
    available = RecipeDefinition(
        name="scratch",
        version="1.0.0",
        description="Scratch.",
        runtimes=frozenset({"pipecat"}),
        min_engine="0.1.0",
        source_available=True,
    )
    registry = RecipeRegistry((available,), capabilities=capabilities)

    assert registry.get("scratch") == available
    assert registry.list(include_unavailable=False) == (available,)
    assert registry.require("scratch", "pipecat") == available
    with pytest.raises(VoicekitError, match="unknown"):
        registry.require("missing", "pipecat")
    with pytest.raises(VoicekitError, match="does not contain"):
        registry.require("scratch", "livekit")

    unavailable_source = RecipeRegistry(
        (
            RecipeDefinition(
                name="scratch",
                version="1.0.0",
                description="Scratch.",
                runtimes=frozenset({"pipecat"}),
                min_engine="0.1.0",
                source_available=False,
            ),
        ),
        capabilities=capabilities,
    )
    with pytest.raises(VoicekitError, match="source is not packaged"):
        unavailable_source.require("scratch", "pipecat")
    with pytest.raises(AssertionError, match="duplicate"):
        RecipeRegistry((available, available), capabilities=capabilities)


def test_env_store_preserves_unrelated_lines_and_is_owner_only(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("# keep\nOTHER='value'\nTOKEN=old\nTOKEN=duplicate\n", encoding="utf-8")
    env_path.chmod(0o644)

    store = EnvFileStore(env_path)
    store.update({"TOKEN": 'new"value', "ADDED": "line\nbreak"})

    payload = env_path.read_text(encoding="utf-8")
    assert payload.count("TOKEN=") == 1
    assert "# keep" in payload
    assert store.read() == {
        "OTHER": "value",
        "TOKEN": 'new"value',
        "ADDED": "line\nbreak",
    }
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_env_protection_is_idempotent_and_process_values_win(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("dist/\n.env*\n", encoding="utf-8")
    ensure_env_ignored(tmp_path)
    ensure_env_ignored(tmp_path)

    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines.count(".env*") == 1
    assert lines.count("!.env.example") == 1
    assert merged_environment({"KEY": "file"}, {"KEY": "process"}) == {"KEY": "process"}


def test_env_store_rejects_links_invalid_names_nul_and_malformed_values(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.write_text('KEY="value"\n', encoding="utf-8")
    link = tmp_path / ".env"
    link.symlink_to(target)

    with pytest.raises(VoicekitError) as read_link:
        EnvFileStore(link).read()
    with pytest.raises(VoicekitError) as update_link:
        EnvFileStore(link).update({"KEY": "value"})
    assert read_link.value.code == "VK-SEC-002"
    assert update_link.value.code == "VK-SEC-002"

    store = EnvFileStore(tmp_path / "safe.env")
    with pytest.raises(VoicekitError) as invalid:
        store.update({"lowercase": "value"})
    with pytest.raises(VoicekitError) as nul:
        store.update({"VALID": "bad\x00value"})
    assert invalid.value.code == "VK-CLI-003"
    assert nul.value.code == "VK-CLI-003"

    store.path.write_text('VALID="unterminated\n', encoding="utf-8")
    with pytest.raises(VoicekitError) as malformed:
        store.read()
    assert malformed.value.code == "VK-CLI-003"


def test_env_decode_supports_export_empty_single_quote_and_inline_comment(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "export EMPTY=\nSINGLE='one two'\nRAW=value # comment\nignored\n",
        encoding="utf-8",
    )

    assert EnvFileStore(path).read() == {
        "EMPTY": "",
        "SINGLE": "one two",
        "RAW": "value",
    }


def test_env_write_and_ignore_os_errors_are_cataloged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_replace(_self: EnvFileStore, _payload: str) -> None:
        raise OSError("disk full")

    store = EnvFileStore(tmp_path / ".env")
    monkeypatch.setattr("voicekit.cli.environment.EnvFileStore._replace", fail_replace)
    with pytest.raises(VoicekitError) as write:
        store.update({"KEY": "value"})
    assert write.value.code == "VK-CLI-003"

    project = tmp_path / "project"
    project.mkdir()
    (project / ".gitignore").mkdir()
    with pytest.raises(VoicekitError) as ignored:
        ensure_env_ignored(project)
    assert ignored.value.code == "VK-CLI-003"


def test_env_ignore_rejects_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "ignore-target"
    target.write_text("", encoding="utf-8")
    (project / ".gitignore").symlink_to(target)

    with pytest.raises(VoicekitError) as caught:
        ensure_env_ignored(project)

    assert caught.value.code == "VK-SEC-002"


@pytest.mark.asyncio
async def test_key_validation_expands_twilio_basic_auth_without_leaking_body() -> None:
    client = FakeHttpClient()
    validator = ProviderKeyValidator(client=client)

    check = await validator.validate(
        "carrier",
        "twilio",
        {
            "TWILIO_ACCOUNT_SID": "AC123",
            "TWILIO_AUTH_TOKEN": "token-value",  # pragma: allowlist secret
        },
    )

    assert check.status == "valid"
    url, headers, timeout_s = client.requests[0]
    assert url.endswith("/Accounts/AC123.json")
    assert headers == {"Authorization": "Basic QUMxMjM6dG9rZW4tdmFsdWU="}
    assert timeout_s == 8


@pytest.mark.asyncio
async def test_key_validation_distinguishes_missing_invalid_and_indeterminate() -> None:
    missing = await ProviderKeyValidator(client=FakeHttpClient()).validate(
        "stt",
        "deepgram/nova-3",
        {},
    )
    invalid = await ProviderKeyValidator(client=FakeHttpClient(401)).validate(
        "stt",
        "deepgram/nova-3",
        {"DEEPGRAM_API_KEY": "bad"},  # pragma: allowlist secret
    )
    indeterminate = await ProviderKeyValidator(client=FakeHttpClient(403)).validate(
        "stt",
        "deepgram/nova-3",
        {"DEEPGRAM_API_KEY": "maybe"},  # pragma: allowlist secret
    )

    assert (missing.status, invalid.status, indeterminate.status) == (
        "missing",
        "invalid",
        "indeterminate",
    )
    assert mask_value("") == "missing"
    assert mask_value("abcd") == "••••"
    assert mask_value("abcdef") == "••••cdef"


@pytest.mark.asyncio
async def test_key_validation_catalogs_timeout_unknown_network_and_template_errors() -> None:
    with pytest.raises(VoicekitError) as timeout:
        ProviderKeyValidator(timeout_s=0)
    assert timeout.value.code == "VK-CLI-004"

    validator = ProviderKeyValidator(client=FakeHttpClient())
    with pytest.raises(VoicekitError) as unknown:
        await validator.validate("llm", "missing/model", {})
    assert unknown.value.code == "VK-CLI-005"

    network = await ProviderKeyValidator(client=RaisingHttpClient()).validate(
        "stt",
        "deepgram/nova-3",
        {"DEEPGRAM_API_KEY": "value"},  # pragma: allowlist secret
    )
    assert network.status == "indeterminate"
    assert "unreachable" in network.detail

    entry = ProviderCatalogEntry(
        id="custom/model",
        kind="llm",
        runtimes=frozenset({"pipecat"}),
        languages=frozenset({"*"}),
        price_class="low",
        latency_class="low",
        key_env_vars=("CUSTOM_KEY",),
        validation_url="https://custom.example.test/${MISSING_URL}",
        validation_headers={"Authorization": "Bearer ${CUSTOM_KEY}"},
        native_idempotency=False,
        description="Custom.",
    )
    custom = ProviderKeyValidator(
        client=FakeHttpClient(),
        catalog=ProviderCatalog((entry,)),
    )
    with pytest.raises(VoicekitError) as template:
        await custom.validate("llm", "custom/model", {"CUSTOM_KEY": "value"})
    assert template.value.code == "VK-CLI-004"


def test_required_entries_deduplicates_one_provider_key() -> None:
    entries = required_entries(
        {
            "stt": "openai/gpt-4o-transcribe",
            "llm": "openai/gpt-5",
            "tts": "openai/gpt-4o-mini-tts",
        },
        carrier=None,
    )
    assert len(entries) == 1
    assert entries[0].key_env_vars == ("OPENAI_API_KEY",)

    with pytest.raises(VoicekitError) as model:
        required_entries(
            {"stt": "missing/model", "llm": "openai/gpt-5", "tts": "openai/gpt-4o-mini-tts"},
            carrier=None,
        )
    with pytest.raises(VoicekitError) as carrier:
        required_entries(
            {
                "stt": "openai/gpt-4o-transcribe",
                "llm": "openai/gpt-5",
                "tts": "openai/gpt-4o-mini-tts",
            },
            carrier="missing",
        )
    assert model.value.code == "VK-CLI-005"
    assert carrier.value.code == "VK-CLI-005"


def test_checkpoint_is_secret_free_and_resumable(tmp_path: Path) -> None:
    store = InitCheckpointStore(tmp_path / "voicekit.jsonc")
    checkpoint = InitCheckpoint(
        project_name="agent",
        answers={"runtime": "pipecat"},
        completed_steps=["runtime"],
    )
    store.save(checkpoint)

    payload = store.path.read_text(encoding="utf-8")
    assert "init-checkpoint" in payload
    assert "secret" not in payload.casefold()
    assert store.load() == checkpoint


def test_checkpoint_catalogs_invalid_input_and_atomic_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "voicekit.jsonc"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(VoicekitError) as invalid:
        InitCheckpointStore(path).load()
    assert invalid.value.code == "VK-CLI-002"

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("voicekit.cli.checkpoint.os.replace", fail_replace)
    with pytest.raises(VoicekitError) as failed:
        InitCheckpointStore(path).save(InitCheckpoint(project_name="agent"))
    assert failed.value.code == "VK-CLI-003"
    assert not list(tmp_path.glob("*.tmp"))


def test_project_discovery_and_next_steps_cover_resume_keys_secret_and_dev(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "src" / "nested"
    nested.mkdir(parents=True)
    checkpoint_path = tmp_path / "voicekit.jsonc"
    InitCheckpointStore(checkpoint_path).save(InitCheckpoint(project_name="agent"))

    checkpoint = discover_project(nested, {})
    assert checkpoint.root == tmp_path
    assert checkpoint.checkpoint
    assert next_step(checkpoint) == "voicekit init --resume"
    with pytest.raises(VoicekitError, match="--resume"):
        require_manifest(checkpoint)

    checkpoint_path.unlink()
    empty = discover_project(nested / "missing.py", {})
    assert empty.root == nested
    assert next_step(empty) == "voicekit init"
    with pytest.raises(VoicekitError, match="voicekit init"):
        require_manifest(empty)

    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="agent",
        runtime="pipecat",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"web"}),
        models=models,
    )
    ManifestStore(checkpoint_path).save(manifest)
    missing_keys = discover_project(nested, {})
    assert require_manifest(missing_keys) == manifest
    assert next_step(missing_keys) == "voicekit keys add deepgram"

    all_keys = {
        "DEEPGRAM_API_KEY": "dg",
        "ANTHROPIC_API_KEY": "ant",
        "CARTESIA_API_KEY": "car",
    }
    missing_secret = discover_project(nested, all_keys)
    assert next_step(missing_secret) == "voicekit doctor --fix"
    ready = discover_project(
        nested,
        {**all_keys, "VOICEKIT_WEBHOOK_SECRET": "whsec_test"},
    )
    assert next_step(ready) == "voicekit dev"

    explicit = ProjectContext(tmp_path, manifest, False, ready.environment)
    assert require_manifest(explicit) is manifest


def test_scratch_scaffold_is_native_compilable_and_manifested(tmp_path: Path) -> None:
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="support-agent",
        runtime="pipecat",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"phone", "web"}),
        models=models,
        carriers=["twilio"],
        phone_number="+14155550123",
        state=ManifestState(last_command="init"),
    )
    scaffold = ScratchScaffold(
        project_name="support-agent",
        description="Help customers troubleshoot orders.",
        stt="deepgram/nova-3",
        llm="anthropic/claude-sonnet-5",
        tts="cartesia/sonic-3.5",
        phone_provider="twilio",
        phone_number="+14155550123",
        web_enabled=True,
    )

    written = ScaffoldWriter().write(tmp_path, scaffold, manifest)

    assert written
    compile((tmp_path / "agent.py").read_text(encoding="utf-8"), "agent.py", "exec")
    compile((tmp_path / "flow.py").read_text(encoding="utf-8"), "flow.py", "exec")
    assert "pipecat.flows" in (tmp_path / "flow.py").read_text(encoding="utf-8")
    all_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in written
        if path.suffix in {".py", ".md", ".toml"}
    )
    assert "flow DSL" not in all_source
    assert "MCP" not in all_source
    assert ManifestStore(tmp_path / "voicekit.jsonc").load() == manifest
    project_data = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert "voicekit[pipecat,twilio]" in project_data


def test_livekit_vobiz_scaffold_declares_every_control_plane_value(tmp_path: Path) -> None:
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="india-agent",
        runtime="livekit",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"phone"}),
        models=models,
        carriers=["vobiz"],
        phone_number="+918071234567",
    )
    scaffold = ScratchScaffold(
        project_name="india-agent",
        description="Handle calls.",
        stt="deepgram/nova-3",
        llm="anthropic/claude-sonnet-5",
        tts="cartesia/sonic-3.5",
        phone_provider="vobiz",
        phone_number="+918071234567",
        web_enabled=False,
        runtime="livekit",
    )

    ScaffoldWriter().write(tmp_path, scaffold, manifest)

    project = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    env = (tmp_path / ".env.example").read_text(encoding="utf-8")
    assert "voicekit[livekit,vobiz]" in project
    assert "VOBIZ_AUTH_ID=" in env
    assert "VOBIZ_AUTH_TOKEN=" in env
    assert "VOICEKIT_VOBIZ_SIP_CREDENTIAL_ID=" in env
    assert "VOICEKIT_VOBIZ_SIP_USERNAME=" in env
    assert "VOICEKIT_VOBIZ_SIP_PASSWORD=" in env


def test_scaffold_refuses_to_overwrite_user_content(tmp_path: Path) -> None:
    (tmp_path / "agent.py").write_text("# mine\n", encoding="utf-8")
    scaffold = ScratchScaffold(
        project_name="support-agent",
        description="Help callers.",
        stt="deepgram/nova-3",
        llm="anthropic/claude-sonnet-5",
        tts="cartesia/sonic-3.5",
        phone_provider=None,
        phone_number=None,
        web_enabled=True,
    )
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="support-agent",
        runtime="pipecat",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"web"}),
        models=models,
    )

    with pytest.raises(VoicekitError) as caught:
        ScaffoldWriter().write(tmp_path, scaffold, manifest)
    assert caught.value.code == "VK-CLI-003"
    assert json.loads(
        json.dumps({"user_file": (tmp_path / "agent.py").read_text(encoding="utf-8")})
    ) == {"user_file": "# mine\n"}


def test_scaffold_rejects_manifest_runtime_mismatch(tmp_path: Path) -> None:
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="mismatch",
        runtime="livekit",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"web"}),
        models=models,
    )
    scaffold = ScratchScaffold(
        project_name="mismatch",
        description="Mismatch.",
        stt=models["stt"],
        llm=models["llm"],
        tts=models["tts"],
        phone_provider=None,
        phone_number=None,
        web_enabled=True,
    )

    with pytest.raises(VoicekitError, match="does not match"):
        ScaffoldWriter().write(tmp_path, scaffold, manifest)


def test_scaffold_merges_gitignore_is_idempotent_and_supports_web_off(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text("dist/\n", encoding="utf-8")
    models: dict[ModelAxis, str] = {
        "stt": "openai/gpt-4o-transcribe",
        "llm": "openai/gpt-5",
        "tts": "openai/gpt-4o-mini-tts",
    }
    manifest = ProjectManifest(
        project_name="web-off",
        runtime="pipecat",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"phone"}),
        models=models,
        carriers=["twilio"],
        phone_number="+14155550123",
    )
    scaffold = ScratchScaffold(
        project_name="web-off",
        description="Place calls.",
        stt=models["stt"],
        llm=models["llm"],
        tts=models["tts"],
        phone_provider="twilio",
        phone_number="+14155550123",
        web_enabled=False,
    )

    writer = ScaffoldWriter()
    assert writer.write(tmp_path, scaffold, manifest)
    assert writer.write(tmp_path, scaffold, manifest) == ()
    assert "dist/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "web=Web()" in (tmp_path / "agent.py").read_text(encoding="utf-8")
    assert (tmp_path / ".env.example").read_text(encoding="utf-8").count("OPENAI_API_KEY=") == 1


def test_scaffold_rejects_gitignore_link_and_rolls_back_manifest_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.write_text("", encoding="utf-8")
    (tmp_path / ".gitignore").symlink_to(target)
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="rollback",
        runtime="pipecat",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"web"}),
        models=models,
    )
    scaffold = ScratchScaffold(
        project_name="rollback",
        description="Roll back.",
        stt=models["stt"],
        llm=models["llm"],
        tts=models["tts"],
        phone_provider=None,
        phone_number=None,
        web_enabled=True,
    )

    with pytest.raises(VoicekitError) as linked:
        ScaffoldWriter().write(tmp_path, scaffold, manifest)
    assert linked.value.code == "VK-SEC-002"
    (tmp_path / ".gitignore").unlink()

    def fail_save(_store: ManifestStore, _manifest: ProjectManifest) -> None:
        raise RuntimeError("manifest write failed")

    monkeypatch.setattr("voicekit.cli.scaffold.ManifestStore.save", fail_save)
    with pytest.raises(RuntimeError, match="manifest write failed"):
        ScaffoldWriter().write(tmp_path, scaffold, manifest)
    assert not (tmp_path / "agent.py").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_scaffold_catalogs_atomic_create_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="disk-full",
        runtime="pipecat",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"web"}),
        models=models,
    )
    scaffold = ScratchScaffold(
        project_name="disk-full",
        description="Fail safely.",
        stt=models["stt"],
        llm=models["llm"],
        tts=models["tts"],
        phone_provider=None,
        phone_number=None,
        web_enabled=True,
    )

    def fail_mkstemp(**_kwargs: object) -> tuple[int, str]:
        raise OSError("disk full")

    monkeypatch.setattr("voicekit.cli.scaffold.tempfile.mkstemp", fail_mkstemp)
    with pytest.raises(VoicekitError) as failed:
        ScaffoldWriter().write(tmp_path, scaffold, manifest)
    assert failed.value.code == "VK-CLI-003"
