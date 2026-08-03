"""Two-tier development reload coordinated at call boundaries."""

from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from watchfiles import awatch  # pyright: ignore[reportUnknownVariableType]

from voicey.config.models import Agent
from voicey.errors import VoiceyError

AgentLoader = Callable[[], Agent]
AgentLoaded = Callable[[Agent], None]


class ReloadableRuntime(Protocol):
    """Minimal runtime hook required by the development watcher."""

    async def reload_agent(self, agent: Agent, *, restart_runner: bool) -> bool: ...


@dataclass(frozen=True, slots=True)
class ReloadSnapshot:
    """Admin-visible watcher state."""

    revision: int
    state: str
    message: str | None

    def as_mapping(self) -> Mapping[str, object]:
        return {
            "revision": self.revision,
            "state": self.state,
            "message": self.message,
        }


class ReloadController:
    """Apply prompt/config changes in process and code changes after a runner restart."""

    def __init__(
        self,
        *,
        root: Path,
        agent_module: str,
        runtime: ReloadableRuntime,
        load_agent: AgentLoader,
        on_loaded: AgentLoaded | None = None,
        retry_s: float = 0.1,
    ) -> None:
        self.root = root.resolve()
        self.agent_module = agent_module
        self.runtime = runtime
        self.load_agent = load_agent
        self.on_loaded = on_loaded
        self.retry_s = retry_s
        self._snapshot = ReloadSnapshot(revision=0, state="ready", message=None)

    def snapshot(self) -> Mapping[str, object]:
        return self._snapshot.as_mapping()

    async def watch(self, stop_event: asyncio.Event) -> None:
        """Watch project files until the dev supervisor begins shutdown."""
        try:
            async for changes in awatch(self.root, stop_event=stop_event):
                changed = _resolve_changes(changes)
                if _relevant(changed, self.root):
                    await self.apply(changed, stop_event=stop_event)
        except OSError as exc:
            self._snapshot = ReloadSnapshot(
                revision=self._snapshot.revision,
                state="error",
                message=f"VY-WEB-005: file watcher failed: {exc}",
            )

    async def apply(
        self,
        changed: set[Path],
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Apply one coalesced file-change batch; exposed for deterministic tests."""
        hard = _requires_worker_restart(changed, self.root)
        next_state = "restart_pending" if hard else "reloading"
        self._snapshot = ReloadSnapshot(
            revision=self._snapshot.revision,
            state=next_state,
            message=(
                "Flow or tool code changed; waiting for the active call to finish."
                if hard
                else "Prompt or agent configuration changed; applying to the next session."
            ),
        )
        try:
            if hard:
                _evict_project_modules(
                    self.root,
                    keep=frozenset({self.agent_module}),
                )
            _evict_module(self.agent_module)
            importlib.invalidate_caches()
            agent = self.load_agent()
            while not await self.runtime.reload_agent(agent, restart_runner=hard):
                if stop_event is not None and stop_event.is_set():
                    return
                await asyncio.sleep(self.retry_s)
            if self.on_loaded is not None:
                self.on_loaded(agent)
        except (ImportError, AttributeError, OSError, VoiceyError, ValueError) as exc:
            self._snapshot = ReloadSnapshot(
                revision=self._snapshot.revision,
                state="error",
                message=f"VY-WEB-005: reload failed: {exc}",
            )
            return
        self._snapshot = ReloadSnapshot(
            revision=self._snapshot.revision + 1,
            state="ready",
            message=("runtime worker restarted" if hard else "configuration loaded"),
        )


def _relevant(changed: set[Path], root: Path) -> bool:
    return any(
        path.suffix == ".py"
        or path.name == "voicey.jsonc"
        or _is_relative_to(path, root / "prompts")
        for path in changed
    )


def _resolve_changes(changes: Iterable[tuple[object, str]]) -> set[Path]:
    return {Path(path).resolve() for _kind, path in changes}


def _requires_worker_restart(changed: set[Path], root: Path) -> bool:
    agent_file = root / "agent.py"
    prompt_root = root / "prompts"
    manifest = root / "voicey.jsonc"
    return any(
        path.suffix == ".py"
        and path != agent_file
        and not _is_relative_to(path, prompt_root)
        and path != manifest
        for path in changed
    )


def _evict_project_modules(root: Path, *, keep: frozenset[str]) -> None:
    for name, module in tuple(sys.modules.items()):
        if name in keep:
            continue
        source = getattr(module, "__file__", None)
        if isinstance(source, str) and _is_relative_to(Path(source).resolve(), root):
            sys.modules.pop(name, None)


def _evict_module(name: str) -> None:
    sys.modules.pop(name, None)
    prefix = f"{name}."
    for loaded in tuple(sys.modules):
        if loaded.startswith(prefix):
            sys.modules.pop(loaded, None)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
