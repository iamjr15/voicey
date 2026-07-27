"""Deployment artifact, persistence, and smoke-verification surfaces."""

from voicekit.deploy.docker import (
    DockerArtifacts,
    DockerDeploymentGenerator,
    DockerSmokeResult,
    DockerSmokeVerifier,
)
from voicekit.deploy.persistence import (
    PersistencePreflightReport,
    RollingGenerationReport,
    docker_persistence_preflight,
    rolling_generation_invariant,
)

__all__ = [
    "DockerArtifacts",
    "DockerDeploymentGenerator",
    "DockerSmokeResult",
    "DockerSmokeVerifier",
    "PersistencePreflightReport",
    "RollingGenerationReport",
    "docker_persistence_preflight",
    "rolling_generation_invariant",
]
