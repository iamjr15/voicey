from __future__ import annotations

import stat
from collections.abc import Mapping
from pathlib import Path

import pytest

from voicekit.cli.drafting import PromptDrafter
from voicekit.cli.keys import KeyCheck
from voicekit.cli.prompts import PromptChoice
from voicekit.cli.wizard import InitOptions, InitWizard
from voicekit.config.catalog import ProviderKind
from voicekit.config.manifest import ManifestStore, RecipeSelection
from voicekit.errors import VoicekitError


class ScriptedPrompt:
    def __init__(
        self,
        *,
        selections: list[str] | None = None,
        multiselections: list[tuple[str, ...]] | None = None,
        texts: list[str] | None = None,
        secrets: list[str] | None = None,
        interactive: bool = True,
    ) -> None:
        self._selections = selections or []
        self._multiselections = multiselections or []
        self._texts = texts or []
        self._secrets = secrets or []
        self._interactive = interactive
        self.notices: list[str] = []
        self.choice_sets: list[tuple[PromptChoice, ...]] = []
        self.text_calls: list[str] = []

    @property
    def interactive(self) -> bool:
        return self._interactive

    def select(self, message: str, choices: tuple[PromptChoice, ...]) -> str:
        del message
        self.choice_sets.append(choices)
        if not self._selections:
            raise VoicekitError("VK-CLI-001", detail="missing selection flag")
        return self._selections.pop(0)

    def multiselect(
        self,
        message: str,
        choices: tuple[PromptChoice, ...],
        *,
        minimum: int = 1,
    ) -> tuple[str, ...]:
        del message
        assert minimum == 1
        self.choice_sets.append(choices)
        if not self._multiselections:
            raise VoicekitError("VK-CLI-001", detail="missing channels flag")
        return self._multiselections.pop(0)

    def text(self, message: str) -> str:
        self.text_calls.append(message)
        if not self._texts:
            raise VoicekitError("VK-CLI-001", detail="missing text flag")
        return self._texts.pop(0)

    def secret(self, message: str) -> str:
        del message
        if not self._secrets:
            raise VoicekitError("VK-CLI-004", detail="missing key")
        return self._secrets.pop(0)

    def notice(self, message: str) -> None:
        self.notices.append(message)


class AcceptingKeyValidator:
    def __init__(self) -> None:
        self.calls: list[tuple[ProviderKind, str, tuple[str, ...]]] = []

    async def validate(
        self,
        kind: ProviderKind,
        identifier: str,
        values: Mapping[str, str],
    ) -> KeyCheck:
        self.calls.append((kind, identifier, tuple(sorted(values))))
        env_names = {
            "deepgram": ("DEEPGRAM_API_KEY",),
            "anthropic": ("ANTHROPIC_API_KEY",),
            "cartesia": ("CARTESIA_API_KEY",),
            "twilio": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
        }[identifier.split("/", maxsplit=1)[0]]
        status = "valid" if all(values.get(name) for name in env_names) else "missing"
        return KeyCheck(
            provider=identifier.split("/", maxsplit=1)[0],
            env_names=env_names,
            status=status,
            detail="checked",
            fix="paste key",
        )


class RejectingKeyValidator:
    async def validate(
        self,
        kind: ProviderKind,
        identifier: str,
        values: Mapping[str, str],
    ) -> KeyCheck:
        del kind, values
        provider = identifier.split("/", maxsplit=1)[0]
        return KeyCheck(
            provider=provider,
            env_names=(
                {
                    "deepgram": "DEEPGRAM_API_KEY",
                    "anthropic": "ANTHROPIC_API_KEY",
                    "cartesia": "CARTESIA_API_KEY",
                }.get(provider, "TWILIO_AUTH_TOKEN"),
            ),
            status="invalid",
            detail="rejected",
            fix="replace",
        )


class AcceptingLiveKitValidator:
    async def validate(self, values: Mapping[str, str]) -> KeyCheck:
        status = (
            "valid"
            if all(
                values.get(name)
                for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
            )
            else "missing"
        )
        return KeyCheck(
            provider="livekit",
            env_names=("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"),
            status=status,
            detail="checked",
            fix="paste LiveKit project credentials",
        )


class RejectingLiveKitValidator:
    async def validate(self, values: Mapping[str, str]) -> KeyCheck:
        del values
        return KeyCheck(
            provider="livekit",
            env_names=("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"),
            status="invalid",
            detail="rejected",
            fix="replace project credentials",
        )


class FakeDrafter(PromptDrafter):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def draft(
        self,
        llm_model: str,
        description: str,
        values: Mapping[str, str],
    ) -> str:
        assert values["ANTHROPIC_API_KEY"]
        self.calls.append((llm_model, description))
        return f"Drafted: {description}"


REFERENCE_MODELS = {
    "stt": "deepgram/nova-3",
    "llm": "anthropic/claude-sonnet-5",
    "tts": "cartesia/sonic-3.5",
}
REFERENCE_ENV = {
    "DEEPGRAM_API_KEY": "dg-key",  # pragma: allowlist secret
    "ANTHROPIC_API_KEY": "ant-key",  # pragma: allowlist secret
    "CARTESIA_API_KEY": "car-key",  # pragma: allowlist secret
}
LIVEKIT_ENV = {
    **REFERENCE_ENV,
    "LIVEKIT_URL": "wss://project.livekit.cloud",
    "LIVEKIT_API_KEY": "livekit-key",  # pragma: allowlist secret
    "LIVEKIT_API_SECRET": "livekit-secret",  # pragma: allowlist secret
}


@pytest.mark.asyncio
async def test_flag_only_web_wizard_produces_working_scaffold(tmp_path: Path) -> None:
    prompt = ScriptedPrompt(interactive=False)
    validator = AcceptingKeyValidator()
    wizard = InitWizard(
        prompt=prompt,
        key_validator=validator,
        environment=REFERENCE_ENV,
    )

    result = await wizard.run(
        tmp_path / "support-agent",
        InitOptions(
            project_name="support-agent",
            recipe="scratch",
            description="Help customers with order questions.",
            channels=("web",),
            runtime="pipecat",
            models=REFERENCE_MODELS,
            draft_prompts=False,
        ),
    )

    assert result.manifest.channels == frozenset({"web"})
    assert result.next_step.endswith("voicekit dev")
    assert (result.project_dir / "agent.py").exists()
    assert (result.project_dir / "flow.py").exists()
    assert stat.S_IMODE((result.project_dir / ".env").stat().st_mode) == 0o600
    env_payload = (result.project_dir / ".env").read_text(encoding="utf-8")
    assert "VOICEKIT_WEBHOOK_SECRET=" in env_payload
    assert "dg-key" not in env_payload
    assert "ant-key" not in env_payload
    assert "car-key" not in env_payload
    assert ManifestStore(result.project_dir / "voicekit.jsonc").load() == result.manifest
    assert len(validator.calls) == 3


@pytest.mark.asyncio
async def test_flag_only_appointment_recipe_copies_authored_native_source(
    tmp_path: Path,
) -> None:
    prompt = ScriptedPrompt(interactive=False)
    wizard = InitWizard(
        prompt=prompt,
        key_validator=AcceptingKeyValidator(),
        environment=REFERENCE_ENV,
    )

    result = await wizard.run(
        tmp_path / "appointments",
        InitOptions(
            project_name="appointments",
            recipe="appointment-booking",
            channels=("web",),
            runtime="pipecat",
            models=REFERENCE_MODELS,
            draft_prompts=False,
        ),
    )

    assert result.manifest.recipe == RecipeSelection(
        name="appointment-booking",
        version="1.0.0",
    )
    assert "appointment-intake" in (result.project_dir / "flow.py").read_text(encoding="utf-8")
    assert (result.project_dir / "eval_bot.py").is_file()
    assert (result.project_dir / "evals" / "text-suite.yaml").is_file()
    assert "VOICEKIT_TRANSFER_NUMBER=" in (result.project_dir / ".env.example").read_text(
        encoding="utf-8"
    )
    assert prompt.text_calls == []


@pytest.mark.asyncio
async def test_flag_only_livekit_appointment_recipe_copies_native_handoffs(
    tmp_path: Path,
) -> None:
    prompt = ScriptedPrompt(interactive=False)
    wizard = InitWizard(
        prompt=prompt,
        key_validator=AcceptingKeyValidator(),
        livekit_validator=AcceptingLiveKitValidator(),
        environment=LIVEKIT_ENV,
    )

    result = await wizard.run(
        tmp_path / "appointments-livekit",
        InitOptions(
            project_name="appointments-livekit",
            recipe="appointment-booking",
            channels=("web",),
            runtime="livekit",
            models=REFERENCE_MODELS,
            draft_prompts=False,
        ),
    )

    assert result.manifest.recipe == RecipeSelection(
        name="appointment-booking",
        version="1.0.0",
    )
    assert result.manifest.runtime == "livekit"
    flow_source = (result.project_dir / "flow.py").read_text(encoding="utf-8")
    assert "class AppointmentIntakeAgent" in flow_source
    assert "GetNameTask" in flow_source
    assert "GetEmailTask" in flow_source
    assert not (result.project_dir / "eval_bot.py").exists()
    assert (result.project_dir / "tests" / "test_recipe.py").is_file()
    assert prompt.text_calls == []


@pytest.mark.asyncio
async def test_interactive_wizard_pastes_validates_and_drafts(tmp_path: Path) -> None:
    prompt = ScriptedPrompt(
        selections=[
            "scratch",
            "pipecat",
            "deepgram/nova-3",
            "anthropic/claude-sonnet-5",
            "cartesia/sonic-3.5",
            "yes",
        ],
        multiselections=[("web",)],
        texts=["Help callers understand a product."],
        secrets=["dg", "anthropic", "cartesia"],  # pragma: allowlist secret
    )
    validator = AcceptingKeyValidator()
    drafter = FakeDrafter()
    wizard = InitWizard(
        prompt=prompt,
        key_validator=validator,
        drafter=drafter,
        environment={},
    )

    result = await wizard.run(
        tmp_path / "draft-agent",
        InitOptions(project_name="draft-agent"),
    )

    assert drafter.calls == [("anthropic/claude-sonnet-5", "Help callers understand a product.")]
    assert (
        (result.project_dir / "prompts/system.md")
        .read_text(encoding="utf-8")
        .startswith("Drafted:")
    )
    env_payload = (result.project_dir / ".env").read_text(encoding="utf-8")
    assert "DEEPGRAM_API_KEY" in env_payload
    assert "ANTHROPIC_API_KEY" in env_payload
    assert "CARTESIA_API_KEY" in env_payload
    assert any(not choice.disabled_reason for choice in prompt.choice_sets[0])
    assert prompt.choice_sets[0][-1].value == "scratch"


@pytest.mark.asyncio
async def test_resume_uses_checkpoint_without_reasking_saved_answers(tmp_path: Path) -> None:
    project_dir = tmp_path / "resume-agent"
    first = InitWizard(
        prompt=ScriptedPrompt(interactive=False),
        key_validator=AcceptingKeyValidator(),
        environment=REFERENCE_ENV,
    )
    with pytest.raises(VoicekitError) as missing:
        await first.run(
            project_dir,
            InitOptions(
                project_name="resume-agent",
                recipe="scratch",
                description="Resume this agent.",
            ),
        )
    assert missing.value.code == "VK-CLI-001"
    checkpoint_payload = (project_dir / "voicekit.jsonc").read_text(encoding="utf-8")
    assert "Resume this agent." in checkpoint_payload

    second = InitWizard(
        prompt=ScriptedPrompt(interactive=False),
        key_validator=AcceptingKeyValidator(),
        environment=REFERENCE_ENV,
    )
    result = await second.run(
        project_dir,
        InitOptions(
            project_name="resume-agent",
            channels=("web",),
            runtime="pipecat",
            models=REFERENCE_MODELS,
            draft_prompts=False,
            resume=True,
        ),
    )
    assert result.manifest.project_name == "resume-agent"


@pytest.mark.asyncio
async def test_unknown_runtime_fails_before_key_or_scaffold_writes(tmp_path: Path) -> None:
    wizard = InitWizard(
        prompt=ScriptedPrompt(interactive=False),
        key_validator=AcceptingKeyValidator(),
        environment=REFERENCE_ENV,
    )
    project_dir = tmp_path / "bad-agent"

    with pytest.raises(VoicekitError) as caught:
        await wizard.run(
            project_dir,
            InitOptions(
                project_name="bad-agent",
                recipe="scratch",
                description="Do a thing.",
                channels=("web",),
                runtime="missing",
                models=REFERENCE_MODELS,
                draft_prompts=False,
            ),
        )

    assert caught.value.code == "VK-CLI-005"
    assert not (project_dir / ".env").exists()
    assert not (project_dir / "agent.py").exists()


@pytest.mark.asyncio
async def test_livekit_web_wizard_produces_native_quickstart(tmp_path: Path) -> None:
    wizard = InitWizard(
        prompt=ScriptedPrompt(interactive=False),
        key_validator=AcceptingKeyValidator(),
        livekit_validator=AcceptingLiveKitValidator(),
        environment=LIVEKIT_ENV,
    )

    result = await wizard.run(
        tmp_path / "livekit-agent",
        InitOptions(
            project_name="livekit-agent",
            recipe="scratch",
            description="Help callers with product questions.",
            channels=("web",),
            runtime="livekit",
            models=REFERENCE_MODELS,
            draft_prompts=False,
        ),
    )

    assert result.manifest.runtime == "livekit"
    assert "runtime='livekit'" in (result.project_dir / "agent.py").read_text(encoding="utf-8")
    flow = (result.project_dir / "flow.py").read_text(encoding="utf-8")
    assert "from livekit.agents import Agent, FunctionTool" in flow
    assert "pipecat.flows" not in flow
    assert "voicekit[livekit]" in (result.project_dir / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    env_example = (result.project_dir / ".env.example").read_text(encoding="utf-8")
    assert "LIVEKIT_URL=" in env_example
    assert "LIVEKIT_API_SECRET=" in env_example
    env_payload = (result.project_dir / ".env").read_text(encoding="utf-8")
    assert "livekit-secret" not in env_payload


@pytest.mark.asyncio
async def test_livekit_wizard_collects_valid_credentials_in_flow(tmp_path: Path) -> None:
    prompt = ScriptedPrompt(
        texts=["wss://project.livekit.cloud"],
        secrets=["livekit-key", "livekit-secret"],  # pragma: allowlist secret
    )
    wizard = InitWizard(
        prompt=prompt,
        key_validator=AcceptingKeyValidator(),
        livekit_validator=AcceptingLiveKitValidator(),
        environment=REFERENCE_ENV,
    )

    result = await wizard.run(
        tmp_path / "livekit-collected",
        InitOptions(
            project_name="livekit-collected",
            recipe="scratch",
            description="Collect credentials.",
            channels=("web",),
            runtime="livekit",
            models=REFERENCE_MODELS,
            draft_prompts=False,
        ),
    )

    env = (result.project_dir / ".env").read_text(encoding="utf-8")
    assert "LIVEKIT_URL" in env
    assert "LIVEKIT_API_KEY" in env
    assert "LIVEKIT_API_SECRET" in env
    assert any("livekit credentials validated" in notice for notice in prompt.notices)


@pytest.mark.asyncio
async def test_livekit_wizard_key_failures_are_actionable(tmp_path: Path) -> None:
    options = InitOptions(
        project_name="livekit-bad",
        recipe="scratch",
        description="Reject credentials.",
        channels=("web",),
        runtime="livekit",
        models=REFERENCE_MODELS,
        draft_prompts=False,
    )
    noninteractive = InitWizard(
        prompt=ScriptedPrompt(interactive=False),
        key_validator=AcceptingKeyValidator(),
        livekit_validator=RejectingLiveKitValidator(),
        environment=REFERENCE_ENV,
    )
    with pytest.raises(VoicekitError, match="keys add livekit"):
        await noninteractive.run(tmp_path / "livekit-noninteractive", options)

    process_values = {
        **REFERENCE_ENV,
        "LIVEKIT_URL": "wss://bad.livekit.cloud",
        "LIVEKIT_API_KEY": "bad",  # pragma: allowlist secret
        "LIVEKIT_API_SECRET": "bad",  # pragma: allowlist secret
    }
    process = InitWizard(
        prompt=ScriptedPrompt(),
        key_validator=AcceptingKeyValidator(),
        livekit_validator=RejectingLiveKitValidator(),
        environment=process_values,
    )
    with pytest.raises(VoicekitError, match="process environment"):
        await process.run(tmp_path / "livekit-process", options)

    blank = InitWizard(
        prompt=ScriptedPrompt(texts=[""], secrets=["key", "secret"]),
        key_validator=AcceptingKeyValidator(),
        livekit_validator=RejectingLiveKitValidator(),
        environment=REFERENCE_ENV,
    )
    with pytest.raises(VoicekitError, match="cannot be blank"):
        await blank.run(tmp_path / "livekit-blank", options)

    exhausted = InitWizard(
        prompt=ScriptedPrompt(
            texts=["wss://bad"] * 3,
            secrets=["bad"] * 6,
        ),
        key_validator=AcceptingKeyValidator(),
        livekit_validator=RejectingLiveKitValidator(),
        environment=REFERENCE_ENV,
    )
    with pytest.raises(VoicekitError, match="failed three validation attempts"):
        await exhausted.run(tmp_path / "livekit-exhausted", options)


@pytest.mark.asyncio
async def test_noninteractive_missing_keys_names_add_command(tmp_path: Path) -> None:
    wizard = InitWizard(
        prompt=ScriptedPrompt(interactive=False),
        key_validator=AcceptingKeyValidator(),
        environment={},
    )

    with pytest.raises(VoicekitError) as caught:
        await wizard.run(
            tmp_path / "missing-key",
            InitOptions(
                project_name="missing-key",
                recipe="scratch",
                description="Do a thing.",
                channels=("web",),
                runtime="pipecat",
                models=REFERENCE_MODELS,
                draft_prompts=False,
            ),
        )
    assert caught.value.code == "VK-CLI-004"
    assert caught.value.detail is not None
    assert "voicekit keys add deepgram" in caught.value.detail


@pytest.mark.asyncio
async def test_phone_wizard_selects_carrier_number_and_persists_resume_state(
    tmp_path: Path,
) -> None:
    prompt = ScriptedPrompt(
        selections=["twilio"],
        texts=["+14155550123"],
    )
    environment = {
        **REFERENCE_ENV,
        "TWILIO_ACCOUNT_SID": "AC" + "1" * 32,
        "TWILIO_AUTH_TOKEN": "twilio-token",  # pragma: allowlist secret
    }
    wizard = InitWizard(
        prompt=prompt,
        key_validator=AcceptingKeyValidator(),
        environment=environment,
    )
    project_dir = tmp_path / "phone-agent"

    result = await wizard.run(
        project_dir,
        InitOptions(
            project_name="phone-agent",
            recipe="scratch",
            description="Answer phone questions.",
            channels=("phone", "web"),
            runtime="pipecat",
            models=REFERENCE_MODELS,
            draft_prompts=False,
        ),
    )

    assert result.manifest.phone_number == "+14155550123"
    assert result.manifest.carriers == ["twilio"]
    assert "voicekit[pipecat,twilio]" in (project_dir / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    with pytest.raises(VoicekitError) as duplicate:
        await wizard.run(project_dir, InitOptions(resume=True))
    assert duplicate.value.code == "VK-CLI-007"


@pytest.mark.asyncio
async def test_wizard_rejects_invalid_phone_model_axis_and_missing_extra(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    wizard = InitWizard(
        prompt=ScriptedPrompt(interactive=False),
        key_validator=AcceptingKeyValidator(),
        environment=REFERENCE_ENV,
    )

    with pytest.raises(VoicekitError) as phone:
        await wizard.run(
            tmp_path / "invalid-agent",
            InitOptions(
                project_name="invalid-agent",
                recipe="scratch",
                description="Help.",
                channels=("phone",),
                phone_provider="twilio",
                phone_number="not-e164",
                runtime="pipecat",
                models=REFERENCE_MODELS,
                draft_prompts=False,
            ),
        )
    assert phone.value.code == "VK-CLI-001"

    with pytest.raises(VoicekitError) as axis:
        await wizard.run(
            tmp_path / "axis-agent",
            InitOptions(
                project_name="axis-agent",
                recipe="scratch",
                description="Help.",
                channels=("web",),
                runtime="pipecat",
                models={**REFERENCE_MODELS, "embedding": "provider/model"},
                draft_prompts=False,
            ),
        )
    assert axis.value.code == "VK-CLI-001"

    def no_extra(_module: str) -> None:
        return None

    monkeypatch.setattr("voicekit.cli.wizard.importlib.util.find_spec", no_extra)
    with pytest.raises(VoicekitError) as extra:
        await wizard.run(
            tmp_path / "extra-agent",
            InitOptions(
                project_name="extra-agent",
                recipe="scratch",
                description="Help.",
                channels=("web",),
                runtime="pipecat",
                models=REFERENCE_MODELS,
                draft_prompts=False,
            ),
        )
    assert extra.value.code == "VK-CLI-005"
    assert "voicekit[pipecat]" in str(extra.value)


@pytest.mark.asyncio
async def test_resume_requires_flag_and_matching_name(tmp_path: Path) -> None:
    project_dir = tmp_path / "checkpoint-agent"
    wizard = InitWizard(
        prompt=ScriptedPrompt(interactive=False),
        key_validator=AcceptingKeyValidator(),
        environment=REFERENCE_ENV,
    )
    with pytest.raises(VoicekitError):
        await wizard.run(
            project_dir,
            InitOptions(
                project_name="checkpoint-agent",
                recipe="scratch",
                description="Saved.",
            ),
        )

    with pytest.raises(VoicekitError) as no_resume:
        await wizard.run(project_dir, InitOptions())
    with pytest.raises(VoicekitError) as mismatch:
        await wizard.run(
            project_dir,
            InitOptions(project_name="different", resume=True),
        )

    assert no_resume.value.code == "VK-CLI-002"
    assert mismatch.value.code == "VK-CLI-002"


@pytest.mark.asyncio
async def test_invalid_process_key_and_blank_paste_fail_closed(tmp_path: Path) -> None:
    options = InitOptions(
        project_name="bad-key",
        recipe="scratch",
        description="Help.",
        channels=("web",),
        runtime="pipecat",
        models=REFERENCE_MODELS,
        draft_prompts=False,
    )
    process_key = InitWizard(
        prompt=ScriptedPrompt(interactive=True),
        key_validator=RejectingKeyValidator(),
        environment={"DEEPGRAM_API_KEY": "bad"},  # pragma: allowlist secret
    )
    with pytest.raises(VoicekitError) as process:
        await process_key.run(tmp_path / "process-key", options)
    assert "process environment" in str(process.value)

    blank = InitWizard(
        prompt=ScriptedPrompt(secrets=[""], interactive=True),
        key_validator=RejectingKeyValidator(),
        environment={},
    )
    with pytest.raises(VoicekitError) as empty:
        await blank.run(
            tmp_path / "blank-key",
            InitOptions(
                project_name="blank-key",
                recipe="scratch",
                description="Help.",
                channels=("web",),
                runtime="pipecat",
                models=REFERENCE_MODELS,
                draft_prompts=False,
            ),
        )
    assert "cannot be blank" in str(empty.value)
