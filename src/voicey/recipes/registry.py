"""Static offline recipe index; recipe sources are copied, never hidden remotely."""

from __future__ import annotations

from dataclasses import dataclass

from voicey.capabilities import DEFAULT_CAPABILITIES, CapabilityRegistry
from voicey.config.models import RuntimeName
from voicey.errors import VoiceyError


@dataclass(frozen=True, slots=True)
class RecipeDefinition:
    """Versioned recipe facts displayed by `recipes list` and the wizard."""

    name: str
    version: str
    description: str
    runtimes: frozenset[RuntimeName]
    min_engine: str
    source_available: bool


class RecipeRegistry:
    """Deterministic recipe lookup tied to the implemented capability registry."""

    def __init__(
        self,
        recipes: tuple[RecipeDefinition, ...],
        *,
        capabilities: CapabilityRegistry = DEFAULT_CAPABILITIES,
    ) -> None:
        self._recipes = tuple(sorted(recipes, key=lambda recipe: recipe.name))
        self._indexed = {recipe.name: recipe for recipe in self._recipes}
        if len(self._indexed) != len(self._recipes):
            raise AssertionError("duplicate recipe registry entry")
        self._capabilities = capabilities

    def list(self, *, include_unavailable: bool = True) -> tuple[RecipeDefinition, ...]:
        if include_unavailable:
            return self._recipes
        return tuple(
            recipe
            for recipe in self._recipes
            if (
                (capability := self._capabilities.get("recipe", recipe.name)) is not None
                and capability.enabled
                and recipe.source_available
            )
        )

    def get(self, name: str) -> RecipeDefinition | None:
        return self._indexed.get(name)

    def require(self, name: str, runtime: RuntimeName) -> RecipeDefinition:
        recipe = self.get(name)
        if recipe is None:
            raise VoiceyError("VY-CLI-005", detail=f"recipe {name!r} is unknown.")
        self._capabilities.require("recipe", name)
        if runtime not in recipe.runtimes:
            raise VoiceyError(
                "VY-CLI-005",
                detail=f"recipe {name!r} does not contain a {runtime} variant.",
            )
        if not recipe.source_available:
            raise VoiceyError(
                "VY-CLI-005",
                detail=f"recipe {name!r} source is not packaged in this build.",
            )
        return recipe


DEFAULT_RECIPE_REGISTRY = RecipeRegistry(
    (
        RecipeDefinition(
            name="appointment-booking",
            version="1.0.0",
            description="Book, reschedule, and cancel appointments through a calendar stub.",
            runtimes=frozenset({"pipecat", "livekit"}),
            min_engine="0.1.0",
            source_available=True,
        ),
        RecipeDefinition(
            name="front-desk",
            version="1.0.0",
            description="Answer, triage, take messages, and warm transfer.",
            runtimes=frozenset({"pipecat", "livekit"}),
            min_engine="0.1.0",
            source_available=True,
        ),
        RecipeDefinition(
            name="lead-intake",
            version="1.0.0",
            description="Qualify inquiries, capture consented leads, and schedule follow-up.",
            runtimes=frozenset({"pipecat", "livekit"}),
            min_engine="0.1.0",
            source_available=True,
        ),
        RecipeDefinition(
            name="restaurant-reservations",
            version="1.0.0",
            description="Reserve tables and offer an explicit waitlist fallback.",
            runtimes=frozenset({"pipecat", "livekit"}),
            min_engine="0.1.0",
            source_available=True,
        ),
    )
)
