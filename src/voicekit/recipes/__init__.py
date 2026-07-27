"""Offline first-party recipe metadata."""

from voicekit.recipes.registry import DEFAULT_RECIPE_REGISTRY, RecipeDefinition, RecipeRegistry
from voicekit.recipes.source import install_recipe, recipe_files

__all__ = [
    "DEFAULT_RECIPE_REGISTRY",
    "RecipeDefinition",
    "RecipeRegistry",
    "install_recipe",
    "recipe_files",
]
