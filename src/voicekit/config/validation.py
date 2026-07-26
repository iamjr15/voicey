"""Fix-carrying validation layered over the structural Pydantic schema."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from voicekit.config.catalog import (
    DEFAULT_PROVIDER_CATALOG,
    ProviderCatalog,
    ProviderCatalogEntry,
)
from voicekit.config.models import Agent
from voicekit.errors import VoicekitError
from voicekit.results.signing import WebhookSigner


class ConfigIssue(BaseModel):
    """One validation failure with a stable code, field path, and direct fix."""

    model_config = ConfigDict(frozen=True)

    code: str
    path: str
    message: str
    fix: str


class ConfigValidationError(VoicekitError):
    """Aggregate all actionable configuration issues in one pass."""

    def __init__(self, issues: tuple[ConfigIssue, ...]) -> None:
        self.issues = issues
        detail = " ".join(f"{issue.path}: {issue.message} Fix: {issue.fix}" for issue in issues)
        super().__init__("VK-CFG-001", detail=detail)


def collect_config_issues(
    agent: Agent,
    *,
    environ: Mapping[str, str],
    catalog: ProviderCatalog = DEFAULT_PROVIDER_CATALOG,
) -> tuple[ConfigIssue, ...]:
    """Collect catalog, language, and secret issues without short-circuiting."""
    issues: list[ConfigIssue] = []
    selected: list[tuple[Literal["stt", "llm", "tts"], str, str]] = [
        ("stt", agent.models.stt, "models.stt"),
        ("llm", agent.models.llm, "models.llm"),
        ("tts", agent.models.tts, "models.tts"),
    ]
    selected.extend(
        (axis, identifier, f"models.fallbacks.{axis}")
        for axis, identifier in agent.models.fallbacks.items()
    )

    entries: list[ProviderCatalogEntry] = []
    for kind, identifier, path in selected:
        entry = catalog.get(kind, identifier)
        if entry is None:
            alternatives = catalog.alternatives(kind, agent.runtime, agent.voice.language)
            issues.append(
                ConfigIssue(
                    code="VK-CFG-101",
                    path=path,
                    message=f"{identifier!r} is not in the {kind} catalog.",
                    fix=f"Choose one of: {', '.join(alternatives) or 'no compatible entries'}.",
                )
            )
            continue
        entries.append(entry)
        _check_runtime(entry, agent, path, issues, catalog)
        _check_language(entry, agent, path, issues, catalog)

    if agent.phone is not None:
        carrier = catalog.get("carrier", agent.phone.provider)
        if carrier is None:
            issues.append(
                ConfigIssue(
                    code="VK-CFG-104",
                    path="phone.provider",
                    message=f"{agent.phone.provider!r} is not in the carrier catalog.",
                    fix="Choose a carrier exposed by the implemented-capability registry.",
                )
            )
        else:
            entries.append(carrier)
            _check_runtime(carrier, agent, "phone.provider", issues, catalog)

    key_owners = {
        env_name: entry.id.split("/", maxsplit=1)[0]
        for entry in entries
        for env_name in entry.key_env_vars
    }
    key_owners[agent.results.secret_env] = "webhook"  # pragma: allowlist secret
    if agent.results.previous_secret_env is not None:
        key_owners[agent.results.previous_secret_env] = (  # pragma: allowlist secret
            "webhook"
        )
    for env_name, owner in sorted(key_owners.items()):
        if not environ.get(env_name):
            issues.append(
                ConfigIssue(
                    code="VK-CFG-105",
                    path=f"env.{env_name}",
                    message=f"{env_name} is missing.",
                    fix=f"Run `voicekit keys add {owner}` or inject {env_name} in CI.",
                )
            )

    for path, env_name in (
        ("results.secret_env", agent.results.secret_env),
        ("results.previous_secret_env", agent.results.previous_secret_env),
    ):
        if env_name is None or not environ.get(env_name):
            continue
        try:
            WebhookSigner(environ[env_name])
        except VoicekitError:
            issues.append(
                ConfigIssue(
                    code="VK-CFG-106",
                    path=path,
                    message=f"{env_name} is not a valid whsec_ secret.",
                    fix=f"Rotate it with `voicekit keys add webhook --env {env_name}`.",
                )
            )

    return tuple(issues)


def validate_agent_config(
    agent: Agent,
    *,
    environ: Mapping[str, str],
    catalog: ProviderCatalog = DEFAULT_PROVIDER_CATALOG,
) -> Agent:
    """Return the config when valid; otherwise raise all fix-carrying issues."""
    issues = collect_config_issues(agent, environ=environ, catalog=catalog)
    if issues:
        raise ConfigValidationError(issues)
    return agent


def _check_runtime(
    entry: ProviderCatalogEntry,
    agent: Agent,
    path: str,
    issues: list[ConfigIssue],
    catalog: ProviderCatalog,
) -> None:
    if agent.runtime in entry.runtimes:
        return
    alternatives = catalog.alternatives(entry.kind, agent.runtime, agent.voice.language)
    issues.append(
        ConfigIssue(
            code="VK-CFG-102",
            path=path,
            message=f"{entry.id!r} does not support the {agent.runtime} runtime.",
            fix=f"Choose one of: {', '.join(alternatives) or 'no compatible entries'}.",
        )
    )


def _check_language(
    entry: ProviderCatalogEntry,
    agent: Agent,
    path: str,
    issues: list[ConfigIssue],
    catalog: ProviderCatalog,
) -> None:
    if entry.kind == "carrier":
        return
    languages = [agent.voice.language]
    if agent.voice.fallback_language is not None:
        languages.append(agent.voice.fallback_language)
    for language in languages:
        if entry.supports_language(language):
            continue
        alternatives = catalog.alternatives(entry.kind, agent.runtime, language)
        issues.append(
            ConfigIssue(
                code="VK-CFG-103",
                path=path,
                message=f"{entry.id!r} does not serve language {language!r}.",
                fix=f"Choose one of: {', '.join(alternatives) or 'no compatible entries'}.",
            )
        )
