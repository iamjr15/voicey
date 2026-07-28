"""Deployment artifact, persistence, and smoke-verification surfaces."""

from voicekit.deploy.docker import (
    DockerArtifacts,
    DockerDeploymentGenerator,
    DockerSmokeResult,
    DockerSmokeVerifier,
)
from voicekit.deploy.fly import (
    FlyArtifactGenerator,
    FlyArtifacts,
    FlyctlRunner,
    FlyDeploymentManager,
    FlyDeploymentReport,
    FlyPlan,
    FlyResourceState,
    FlyResourceStore,
    FlySmokeReport,
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
    "FlyArtifactGenerator",
    "FlyArtifacts",
    "FlyDeploymentManager",
    "FlyDeploymentReport",
    "FlyPlan",
    "FlyResourceState",
    "FlyResourceStore",
    "FlySmokeReport",
    "FlyctlRunner",
    "PersistencePreflightReport",
    "RollingGenerationReport",
    "docker_persistence_preflight",
    "rolling_generation_invariant",
]
