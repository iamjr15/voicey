"""Typed configuration, catalog validation, and project manifest."""

from voicey.config.catalog import (
    CURATED_DEFAULT_VOICE_IDS,
    DEFAULT_PROVIDER_CATALOG,
    ProviderCatalog,
    ProviderCatalogEntry,
    resolve_voice_id,
)
from voicey.config.manifest import (
    ManifestState,
    ManifestStore,
    ProjectManifest,
    RecipeSelection,
)
from voicey.config.models import (
    Agent,
    Behavior,
    Limits,
    Models,
    Observability,
    Phone,
    Results,
    Voice,
    Web,
)
from voicey.config.validation import (
    ConfigIssue,
    ConfigValidationError,
    collect_config_issues,
    validate_agent_config,
)

__all__ = [
    "CURATED_DEFAULT_VOICE_IDS",
    "DEFAULT_PROVIDER_CATALOG",
    "Agent",
    "Behavior",
    "ConfigIssue",
    "ConfigValidationError",
    "Limits",
    "ManifestState",
    "ManifestStore",
    "Models",
    "Observability",
    "Phone",
    "ProjectManifest",
    "ProviderCatalog",
    "ProviderCatalogEntry",
    "RecipeSelection",
    "Results",
    "Voice",
    "Web",
    "collect_config_issues",
    "resolve_voice_id",
    "validate_agent_config",
]
