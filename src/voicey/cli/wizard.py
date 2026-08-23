"""Resumable, neutral guided project initialization."""

from __future__ import annotations

import importlib.util
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import JsonValue, ValidationError

from voicey.capabilities import DEFAULT_CAPABILITIES, CapabilityRegistry
from voicey.cli.checkpoint import InitCheckpoint, InitCheckpointStore
from voicey.cli.drafting import PromptDrafter, ProviderPromptDrafter
from voicey.cli.environment import EnvFileStore, ensure_env_ignored, merged_environment
from voicey.cli.keys import (
    LIVEKIT_ENV_VARS,
    KeyValidator,
    LiveKitKeyValidator,
    ProviderKeyValidator,
    RuntimeKeyValidator,
    required_entries,
)
from voicey.cli.prompts import PromptChoice, PromptIO
from voicey.cli.scaffold import ScaffoldWriter, ScratchScaffold
from voicey.config.catalog import (
    DEFAULT_PROVIDER_CATALOG,
    ProviderCatalog,
    ProviderCatalogEntry,
    ProviderKind,
)
from voicey.config.manifest import (
    ChannelName,
    ManifestState,
    ManifestStore,
    ProjectManifest,
    RecipeSelection,
)
from voicey.config.models import ModelAxis, Phone, PhoneProvider, RuntimeName
from voicey.errors import VoiceyError
from voicey.recipes.registry import DEFAULT_RECIPE_REGISTRY, RecipeRegistry
from voicey.results.signing import encode_secret

_EXTRA_IMPORT_MODULES = {
    "pipecat": "pipecat",
    "livekit": "livekit",
    "twilio": "twilio",
    "telnyx": "cryptography",
    "vobiz": "multipart",
    "plivo": "plivo",
}
_PROJECT_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")


@dataclass(frozen=True, slots=True)
class InitOptions:
    """Every guided question's deterministic flag twin."""

    project_name: str | None = None
    recipe: str | None = None
    description: str | None = None
    channels: tuple[str, ...] | None = None
    phone_provider: str | None = None
    phone_number: str | None = None
    runtime: str | None = None
    models: Mapping[str, str] | None = None
    draft_prompts: bool | None = None
    resume: bool = False


@dataclass(frozen=True, slots=True)
class InitResult:
    project_dir: Path
    manifest: ProjectManifest
    written: tuple[Path, ...]
    next_step: str


class InitWizard:
    """Checkpoint every non-secret answer and complete only after live validation."""

    def __init__(
        self,
        *,
        prompt: PromptIO,
        key_validator: KeyValidator | None = None,
        livekit_validator: RuntimeKeyValidator | None = None,
        drafter: PromptDrafter | None = None,
        capabilities: CapabilityRegistry = DEFAULT_CAPABILITIES,
        recipes: RecipeRegistry = DEFAULT_RECIPE_REGISTRY,
        catalog: ProviderCatalog = DEFAULT_PROVIDER_CATALOG,
        environment: Mapping[str, str] | None = None,
        scaffold_writer: ScaffoldWriter | None = None,
    ) -> None:
        self._prompt = prompt
        self._key_validator = key_validator or ProviderKeyValidator()
        self._livekit_validator = livekit_validator or LiveKitKeyValidator()
        self._drafter = drafter or ProviderPromptDrafter()
        self._capabilities = capabilities
        self._recipes = recipes
        self._catalog = catalog
        self._process_environment = dict(os.environ if environment is None else environment)
        self._scaffold_writer = scaffold_writer or ScaffoldWriter()

    async def run(self, project_dir: Path, options: InitOptions) -> InitResult:
        project_dir = _resolve_path(project_dir)
        manifest_path = project_dir / "voicey.jsonc"
        checkpoint_store = InitCheckpointStore(manifest_path)
        checkpoint = self._start_or_resume(project_dir, options, checkpoint_store)

        recipe = self._answer_recipe(checkpoint, options, checkpoint_store)
        if recipe == "scratch":
            description = self._answer_text(
                checkpoint,
                checkpoint_store,
                key="description",
                supplied=options.description,
                prompt="Describe in a sentence or two what your agent should do.",
            )
        else:
            definition = self._recipes.get(recipe)
            if definition is None:
                raise VoiceyError("VY-CLI-005", detail=f"recipe {recipe!r} is unknown.")
            description = options.description or definition.description
        channels = self._answer_channels(checkpoint, options, checkpoint_store)
        carrier: str | None = None
        phone_number: str | None = None
        if "phone" in channels:
            carrier = self._answer_carrier(checkpoint, options, checkpoint_store)
            phone_number = self._answer_phone(checkpoint, options, carrier, checkpoint_store)
        runtime = self._answer_runtime(checkpoint, options, checkpoint_store)
        self._require_carrier_runtime(runtime, carrier)
        recipe_definition = None if recipe == "scratch" else self._recipes.require(recipe, runtime)
        models = self._answer_models(checkpoint, options, runtime, checkpoint_store)
        self._require_installed_extras(runtime, carrier)

        ensure_env_ignored(project_dir)
        env_store = EnvFileStore(project_dir / ".env")
        file_values = env_store.read()
        all_values = merged_environment(file_values, self._process_environment)
        collected = await self._collect_keys(
            models,
            carrier=carrier,
            values=all_values,
            env_store=env_store,
        )
        all_values.update(collected)
        if runtime == "livekit":
            runtime_collected = await self._collect_livekit_keys(
                values=all_values,
                env_store=env_store,
            )
            all_values.update(runtime_collected)
        if not all_values.get("VOICEY_WEBHOOK_SECRET"):
            webhook_secret = encode_secret(secrets.token_bytes(32))
            env_store.update({"VOICEY_WEBHOOK_SECRET": webhook_secret})
            all_values["VOICEY_WEBHOOK_SECRET"] = webhook_secret

        if recipe != "scratch" and options.draft_prompts:
            raise VoiceyError(
                "VY-CLI-001",
                detail="--draft-prompts applies only to the scratch recipe.",
            )
        draft_prompts = (
            self._answer_drafting(checkpoint, options, checkpoint_store)
            if recipe == "scratch"
            else False
        )
        prompt_text = description
        if draft_prompts:
            prompt_text = await self._drafter.draft(models["llm"], description, all_values)

        manifest = ProjectManifest(
            project_name=checkpoint.project_name,
            runtime=runtime,
            recipe=RecipeSelection(
                name=recipe,
                version=("1.0.0" if recipe_definition is None else recipe_definition.version),
            ),
            channels=frozenset(cast("tuple[ChannelName, ...]", channels)),
            models=cast("dict[ModelAxis, str]", models),
            carriers=[] if carrier is None else [carrier],
            phone_number=phone_number,
            state=ManifestState(
                completed_steps=[
                    "recipe",
                    "description",
                    "channels",
                    "runtime",
                    "models",
                    "keys",
                    "scaffold",
                ],
                last_command="init",
            ),
        )
        scaffold = ScratchScaffold(
            project_name=checkpoint.project_name,
            description=prompt_text,
            stt=models["stt"],
            llm=models["llm"],
            tts=models["tts"],
            phone_provider=carrier,
            phone_number=phone_number,
            web_enabled="web" in channels,
            runtime=runtime,
            recipe_name=recipe,
            recipe_version=("1.0.0" if recipe_definition is None else recipe_definition.version),
        )
        written = self._scaffold_writer.write(project_dir, scaffold, manifest)
        return InitResult(
            project_dir=project_dir,
            manifest=manifest,
            written=written,
            next_step=f"cd {project_dir} && voicey dev",
        )

    def _start_or_resume(
        self,
        project_dir: Path,
        options: InitOptions,
        store: InitCheckpointStore,
    ) -> InitCheckpoint:
        path = store.path
        if path.exists():
            try:
                manifest = ManifestStore(path).load()
            except VoiceyError:
                if not options.resume:
                    raise VoiceyError(
                        "VY-CLI-002",
                        detail=f"{path} contains an incomplete setup; pass --resume.",
                    ) from None
                checkpoint = store.load()
                if (
                    options.project_name is not None
                    and options.project_name != checkpoint.project_name
                ):
                    raise VoiceyError(
                        "VY-CLI-002",
                        detail="--name does not match the saved setup checkpoint.",
                    ) from None
                return checkpoint
            raise VoiceyError(
                "VY-CLI-007",
                detail=(
                    f"{manifest.project_name!r} is already initialized. Next: `voicey doctor`."
                ),
            )
        name = options.project_name or project_dir.name
        if not _PROJECT_NAME.fullmatch(name):
            if options.project_name is None:
                name = self._prompt.text("Project name (lowercase letters, digits, dot, - or _):")
            if not _PROJECT_NAME.fullmatch(name):
                raise VoiceyError(
                    "VY-CLI-001",
                    detail="--name must be a lowercase package/project name up to 64 characters.",
                )
        checkpoint = InitCheckpoint(project_name=name)
        store.save(checkpoint)
        return checkpoint

    def _answer_recipe(
        self,
        checkpoint: InitCheckpoint,
        options: InitOptions,
        store: InitCheckpointStore,
    ) -> str:
        saved = _saved_string(checkpoint, "recipe")
        if saved is not None:
            return saved
        supplied = options.recipe
        if supplied is None:
            recipe_choices: list[PromptChoice] = []
            for recipe in self._recipes.list():
                capability = self._capabilities.get("recipe", recipe.name)
                recipe_choices.append(
                    PromptChoice(
                        title=recipe.name,
                        value=recipe.name,
                        description=recipe.description,
                        disabled_reason=(
                            None
                            if (
                                capability is not None
                                and capability.enabled
                                and recipe.source_available
                            )
                            else (
                                capability.unavailable_reason
                                if capability is not None
                                else "not available in this build"
                            )
                        ),
                    ),
                )
            recipe_choices.append(
                PromptChoice(
                    title="Start from scratch",
                    value="scratch",
                    description="A minimal talking agent seeded from your own description.",
                )
            )
            choices = tuple(recipe_choices)
            supplied = self._prompt.select("What should your agent do?", choices)
        self._capabilities.require("recipe", supplied)
        _save_answer(checkpoint, store, "recipe", supplied)
        return supplied

    def _answer_channels(
        self,
        checkpoint: InitCheckpoint,
        options: InitOptions,
        store: InitCheckpointStore,
    ) -> tuple[str, ...]:
        saved = _saved_strings(checkpoint, "channels")
        if saved is not None:
            return saved
        channels = options.channels
        if channels is None:
            channels = self._prompt.multiselect(
                "Where will people talk to it?",
                (
                    PromptChoice(
                        title="Phone",
                        value="phone",
                        description="Inbound/outbound PSTN through the selected carrier.",
                    ),
                    PromptChoice(
                        title="Website / app (browser)",
                        value="web",
                        description="WebRTC audio from the browser playground or your app.",
                    ),
                ),
            )
        normalized = tuple(dict.fromkeys(channel.casefold() for channel in channels))
        if not normalized or any(channel not in {"phone", "web"} for channel in normalized):
            raise VoiceyError(
                "VY-CLI-001",
                detail="--channels requires phone, web, or both.",
            )
        _save_answer(checkpoint, store, "channels", list(normalized))
        return normalized

    def _answer_carrier(
        self,
        checkpoint: InitCheckpoint,
        options: InitOptions,
        store: InitCheckpointStore,
    ) -> str:
        saved = _saved_string(checkpoint, "phone_provider")
        if saved is not None:
            return saved
        carrier = options.phone_provider
        if carrier is None:
            choices = tuple(
                PromptChoice(
                    title=capability.id,
                    value=capability.id,
                    description=capability.description,
                    disabled_reason=None if capability.enabled else capability.unavailable_reason,
                )
                for capability in self._capabilities.choices(
                    "carrier",
                    include_unavailable=True,
                )
            )
            carrier = self._prompt.select("Which phone carrier?", choices)
        self._capabilities.require("carrier", carrier)
        _save_answer(checkpoint, store, "phone_provider", carrier)
        return carrier

    def _answer_phone(
        self,
        checkpoint: InitCheckpoint,
        options: InitOptions,
        carrier: str,
        store: InitCheckpointStore,
    ) -> str:
        saved = _saved_string(checkpoint, "phone_number")
        if saved is not None:
            return saved
        number = options.phone_number
        if number is None:
            number = self._prompt.text(f"Which owned {carrier} number should this agent use?")
        try:
            Phone(provider=cast("PhoneProvider", carrier), number=number)
        except ValidationError as exc:
            raise VoiceyError(
                "VY-CLI-001",
                detail="--phone-number must be an owned E.164 number such as +14155550123.",
            ) from exc
        _save_answer(checkpoint, store, "phone_number", number)
        return number

    def _answer_runtime(
        self,
        checkpoint: InitCheckpoint,
        options: InitOptions,
        store: InitCheckpointStore,
    ) -> RuntimeName:
        saved = _saved_string(checkpoint, "runtime")
        runtime = saved or options.runtime
        if runtime is None:
            choices = tuple(
                PromptChoice(
                    title=capability.id,
                    value=capability.id,
                    description=capability.description,
                    disabled_reason=None if capability.enabled else capability.unavailable_reason,
                )
                for capability in self._capabilities.choices(
                    "runtime",
                    include_unavailable=True,
                )
            )
            runtime = self._prompt.select(
                "Which engine? Every voicey command works identically with either.",
                choices,
            )
        self._capabilities.require("runtime", runtime)
        if saved is None:
            _save_answer(checkpoint, store, "runtime", runtime)
        return cast("RuntimeName", runtime)

    def _answer_models(
        self,
        checkpoint: InitCheckpoint,
        options: InitOptions,
        runtime: RuntimeName,
        store: InitCheckpointStore,
    ) -> dict[str, str]:
        saved = _saved_mapping(checkpoint, "models")
        if saved is not None:
            return saved
        supplied = dict(options.models or {})
        expected = {"stt", "llm", "tts"}
        unknown = set(supplied) - expected
        if unknown:
            raise VoiceyError(
                "VY-CLI-001",
                detail=f"--models contains unknown axis {sorted(unknown)[0]!r}.",
            )
        for axis in ("stt", "llm", "tts"):
            identifier = supplied.get(axis)
            if identifier is None:
                alternatives = self._catalog.alternatives(cast("ProviderKind", axis), runtime)
                identifier = self._prompt.select(
                    f"Choose {axis.upper()}:",
                    tuple(
                        _model_choice(self._catalog.get(cast("ProviderKind", axis), model_id))
                        for model_id in alternatives
                    ),
                )
            entry = self._catalog.get(cast("ProviderKind", axis), identifier)
            if entry is None or runtime not in entry.runtimes:
                raise VoiceyError(
                    "VY-CLI-005",
                    detail=f"{axis} model {identifier!r} does not support {runtime}.",
                )
            supplied[axis] = identifier
        _save_answer(checkpoint, store, "models", supplied)
        return supplied

    async def _collect_keys(
        self,
        models: Mapping[str, str],
        *,
        carrier: str | None,
        values: dict[str, str],
        env_store: EnvFileStore,
    ) -> dict[str, str]:
        collected: dict[str, str] = {}
        for entry in required_entries(models, carrier=carrier, catalog=self._catalog):
            provider = entry.id.split("/", maxsplit=1)[0]
            pending: dict[str, str] = {}
            for attempt in range(4):
                check = await self._key_validator.validate(entry.kind, entry.id, values)
                if check.status == "valid":
                    self._prompt.notice(f"✓ {provider} credentials validated.")
                    break
                self._prompt.notice(f"✖ {provider}: {check.detail} {check.fix}")
                if attempt == 3:
                    raise VoiceyError(
                        "VY-CLI-004",
                        detail=f"{provider} credentials failed three validation attempts.",
                    )
                process_names = tuple(
                    name for name in entry.key_env_vars if self._process_environment.get(name)
                )
                if process_names:
                    raise VoiceyError(
                        "VY-CLI-004",
                        detail=(
                            f"{', '.join(process_names)} comes from the process environment "
                            "and failed validation; replace it there, then resume."
                        ),
                    )
                if not self._prompt.interactive:
                    raise VoiceyError(
                        "VY-CLI-004",
                        detail=(
                            f"{', '.join(entry.key_env_vars)} must be valid. "
                            f"Run `voicey keys add {provider}`."
                        ),
                    )
                pasted = {
                    name: self._prompt.secret(f"Paste {name}:") for name in entry.key_env_vars
                }
                if any(not value for value in pasted.values()):
                    raise VoiceyError(
                        "VY-CLI-004",
                        detail=f"{', '.join(entry.key_env_vars)} cannot be blank.",
                    )
                values.update(pasted)
                pending.update(pasted)
            else:
                raise AssertionError("credential attempt loop did not terminate")
            if pending:
                env_store.update(pending)
                collected.update(pending)
        return collected

    async def _collect_livekit_keys(
        self,
        *,
        values: dict[str, str],
        env_store: EnvFileStore,
    ) -> dict[str, str]:
        pending: dict[str, str] = {}
        for attempt in range(4):
            check = await self._livekit_validator.validate(values)
            if check.status == "valid":
                self._prompt.notice("✓ livekit credentials validated.")
                break
            self._prompt.notice(f"✖ livekit: {check.detail} {check.fix}")
            if attempt == 3:
                raise VoiceyError(
                    "VY-CLI-004",
                    detail="livekit credentials failed three validation attempts.",
                )
            process_names = tuple(
                name for name in LIVEKIT_ENV_VARS if self._process_environment.get(name)
            )
            if process_names:
                raise VoiceyError(
                    "VY-CLI-004",
                    detail=(
                        f"{', '.join(process_names)} comes from the process environment "
                        "and failed validation; replace it there, then resume."
                    ),
                )
            if not self._prompt.interactive:
                raise VoiceyError(
                    "VY-CLI-004",
                    detail=(
                        f"{', '.join(LIVEKIT_ENV_VARS)} must be valid. "
                        "Run `voicey keys add livekit`."
                    ),
                )
            pasted = {
                "LIVEKIT_URL": self._prompt.text("Paste LIVEKIT_URL:"),
                "LIVEKIT_API_KEY": self._prompt.secret("Paste LIVEKIT_API_KEY:"),
                "LIVEKIT_API_SECRET": self._prompt.secret("Paste LIVEKIT_API_SECRET:"),
            }
            if any(not value for value in pasted.values()):
                raise VoiceyError(
                    "VY-CLI-004",
                    detail=f"{', '.join(LIVEKIT_ENV_VARS)} cannot be blank.",
                )
            values.update(pasted)
            pending.update(pasted)
        else:
            raise AssertionError("credential attempt loop did not terminate")
        if pending:
            env_store.update(pending)
        return pending

    def _answer_drafting(
        self,
        checkpoint: InitCheckpoint,
        options: InitOptions,
        store: InitCheckpointStore,
    ) -> bool:
        saved = checkpoint.answers.get("draft_prompts")
        if isinstance(saved, bool):
            return saved
        draft = options.draft_prompts
        if draft is None:
            draft = (
                self._prompt.select(
                    "Draft fuller starting prompts using your configured LLM key?",
                    (
                        PromptChoice(
                            title="Yes",
                            value="yes",
                            description="Makes one paid LLM request using the selected account.",
                        ),
                        PromptChoice(
                            title="No",
                            value="no",
                            description="Uses your description directly; no drafting request.",
                        ),
                    ),
                )
                == "yes"
            )
        _save_answer(checkpoint, store, "draft_prompts", draft)
        return draft

    def _answer_text(
        self,
        checkpoint: InitCheckpoint,
        store: InitCheckpointStore,
        *,
        key: str,
        supplied: str | None,
        prompt: str,
    ) -> str:
        saved = _saved_string(checkpoint, key)
        if saved is not None:
            return saved
        answer = supplied if supplied is not None else self._prompt.text(prompt)
        if not answer.strip():
            raise VoiceyError("VY-CLI-001", detail=f"--{key.replace('_', '-')} cannot be blank.")
        normalized = answer.strip()
        _save_answer(checkpoint, store, key, normalized)
        return normalized

    def _require_installed_extras(self, runtime: str, carrier: str | None) -> None:
        requirements = [(runtime, runtime)]
        if carrier is not None:
            requirements.append(("livekit", "livekit") if carrier == "sip" else (carrier, carrier))
        missing = [
            extra
            for capability, extra in requirements
            if importlib.util.find_spec(_EXTRA_IMPORT_MODULES[capability]) is None
        ]
        if missing:
            extras = ",".join(dict.fromkeys(missing))
            raise VoiceyError(
                "VY-CLI-005",
                detail=f'Install with `uv pip install "voicey[{extras}]"`, then resume init.',
            )

    def _require_carrier_runtime(self, runtime: str, carrier: str | None) -> None:
        if carrier is None:
            return
        entry = self._catalog.get("carrier", carrier)
        if entry is None:
            raise VoiceyError("VY-CLI-005", detail=f"carrier {carrier!r} is absent.")
        if runtime not in entry.runtimes:
            raise VoiceyError(
                "VY-CLI-005",
                detail=(
                    f"carrier {carrier!r} does not support runtime {runtime!r}; "
                    "generic SIP requires LiveKit."
                ),
            )


def _model_choice(entry: ProviderCatalogEntry | None) -> PromptChoice:
    if entry is None:
        raise AssertionError("catalog alternative disappeared")
    identifier = entry.id
    languages = entry.languages
    language_text = "broad language coverage" if "*" in languages else ", ".join(sorted(languages))
    return PromptChoice(
        title=identifier,
        value=identifier,
        description=(
            f"{entry.description} "
            f"Price: {entry.price_class}; "
            f"languages: {language_text}; latency: {entry.latency_class}."
        ),
    )


def _save_answer(
    checkpoint: InitCheckpoint,
    store: InitCheckpointStore,
    key: str,
    value: object,
) -> None:
    checkpoint.answers[key] = cast("JsonValue", value)
    if key not in checkpoint.completed_steps:
        checkpoint.completed_steps.append(key)
    store.save(checkpoint)


def _saved_string(checkpoint: InitCheckpoint, key: str) -> str | None:
    value = checkpoint.answers.get(key)
    return value if isinstance(value, str) else None


def _saved_strings(checkpoint: InitCheckpoint, key: str) -> tuple[str, ...] | None:
    value = checkpoint.answers.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(cast("list[str]", value))


def _saved_mapping(checkpoint: InitCheckpoint, key: str) -> dict[str, str] | None:
    value = checkpoint.answers.get(key)
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
        return None
    return cast("dict[str, str]", value)


def _resolve_path(path: Path) -> Path:
    return path.resolve()
