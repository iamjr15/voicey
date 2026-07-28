"""Bounded dual-runtime lifecycle soak runner."""

from __future__ import annotations

import asyncio
import gc
import importlib
import sys
import time
import tracemalloc
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypeAlias, cast

from voicekit.config.models import Agent, Models, Results, Web
from voicekit.errors import VoicekitError
from voicekit.obs.records import TimelineEvent, TranscriptTurn
from voicekit.runtimes.pipecat.admission import AdmissionController
from voicekit.runtimes.pipecat.lifecycle import (
    PipecatCall,
    PipecatLifecycleManager,
)
from voicekit.storage.sqlite import SQLiteRepository

SoakRuntime: TypeAlias = Literal["pipecat", "livekit"]


@dataclass(frozen=True, slots=True)
class SoakConfig:
    """Resource and wall-clock bounds for one repeatable soak."""

    duration_s: float
    max_concurrent: int
    call_hold_s: float = 0.05
    heap_growth_limit_bytes: int = 32 * 1024 * 1024
    rss_growth_limit_bytes: int = 64 * 1024 * 1024
    fd_growth_limit: int = 4

    def __post_init__(self) -> None:
        if (
            self.duration_s <= 0
            or self.max_concurrent <= 0
            or self.call_hold_s <= 0
            or self.call_hold_s > self.duration_s
            or self.heap_growth_limit_bytes < 0
            or self.rss_growth_limit_bytes < 0
            or self.fd_growth_limit < 0
        ):
            raise VoicekitError(
                "VK-TST-005",
                detail="soak duration, concurrency, hold time, and resource bounds are invalid.",
            )


@dataclass(frozen=True, slots=True)
class SoakReport:
    """Machine-readable proof of bounded lifecycle/resource behavior."""

    duration_s: float
    runtimes: tuple[SoakRuntime, ...]
    max_concurrent: int
    calls_started: int
    calls_completed: int
    terminal_events_verified: int
    peak_active: int
    active_at_end: int
    heap_growth_bytes: int
    peak_heap_growth_bytes: int
    rss_growth_bytes: int | None
    fd_growth: int | None
    failures: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return not self.failures

    def assert_healthy(self) -> None:
        if self.failures:
            raise VoicekitError("VK-TST-005", detail="; ".join(self.failures))


async def run_engine_soak(
    database_path: Path,
    config: SoakConfig,
    *,
    runtimes: tuple[SoakRuntime, ...] = ("pipecat", "livekit"),
) -> SoakReport:
    """Exercise fenced calls at maximum concurrency for the requested duration.

    The synthetic callers persist an input turn, an agent turn, a timeline
    observation, a result value, and one terminal event. This keeps the soak
    deterministic and credential-free while exercising the same shared
    lifecycle used by both native runtime adapters.
    """
    if not runtimes or len(runtimes) != len(set(runtimes)):
        raise VoicekitError("VK-TST-005", detail="soak runtimes must be unique and non-empty.")

    database = await asyncio.to_thread(_prepare_database, database_path)
    tracing_was_active = tracemalloc.is_tracing()
    if not tracing_was_active:
        tracemalloc.start()

    calls_started = 0
    calls_completed = 0
    terminal_verified = 0
    active = 0
    peak_active = 0
    failures: list[str] = []
    counters_lock = asyncio.Lock()

    try:
        async with SQLiteRepository(database) as repository:
            admissions = {
                runtime: AdmissionController(config.max_concurrent) for runtime in runtimes
            }
            managers = {
                runtime: PipecatLifecycleManager(
                    repository,
                    admissions[runtime],
                    runtime=runtime,
                    owner_id=f"soak-{runtime}-{uuid.uuid4().hex}",
                )
                for runtime in runtimes
            }

            # Warm schema/materialization paths before measuring retained growth.
            await _one_soak_call(
                repository,
                managers[runtimes[0]],
                admissions[runtimes[0]],
                runtime=runtimes[0],
                hold_s=min(config.call_hold_s, 0.01),
                sequence=-1,
            )
            gc.collect()
            heap_baseline, _ = tracemalloc.get_traced_memory()
            rss_baseline = _maximum_rss_bytes()
            fd_baseline = _fd_count()
            deadline = time.monotonic() + config.duration_s

            async def worker(runtime: SoakRuntime, slot: int) -> None:
                nonlocal calls_started
                nonlocal calls_completed
                nonlocal terminal_verified
                nonlocal active
                nonlocal peak_active
                sequence = 0
                while time.monotonic() < deadline:
                    async with counters_lock:
                        calls_started += 1
                        active += 1
                        peak_active = max(peak_active, active)
                    try:
                        await _one_soak_call(
                            repository,
                            managers[runtime],
                            admissions[runtime],
                            runtime=runtime,
                            hold_s=config.call_hold_s,
                            sequence=(slot * 1_000_000) + sequence,
                        )
                    except Exception as exc:
                        async with counters_lock:
                            failures.append(f"{runtime} slot {slot}: {type(exc).__name__}")
                    else:
                        async with counters_lock:
                            calls_completed += 1
                            terminal_verified += 1
                    finally:
                        async with counters_lock:
                            active -= 1
                    sequence += 1

            started = time.monotonic()
            await asyncio.gather(
                *(
                    worker(runtime, slot)
                    for runtime in runtimes
                    for slot in range(config.max_concurrent)
                )
            )
            elapsed = time.monotonic() - started
            active_at_end = active
            leaked_admission = sum(item.active_count for item in admissions.values())

            gc.collect()
            heap_current, heap_peak = tracemalloc.get_traced_memory()
            heap_growth = max(0, heap_current - heap_baseline)
            peak_heap_growth = max(0, heap_peak - heap_baseline)
            rss_end = _maximum_rss_bytes()
            rss_growth = (
                None if rss_baseline is None or rss_end is None else max(0, rss_end - rss_baseline)
            )
            fd_end = _fd_count()
            fd_growth = (
                None if fd_baseline is None or fd_end is None else max(0, fd_end - fd_baseline)
            )

        if calls_started == 0:
            failures.append("no calls started")
        if calls_completed != calls_started:
            failures.append(
                f"{calls_started - calls_completed} calls did not complete successfully"
            )
        if terminal_verified != calls_completed:
            failures.append("terminal verification count did not match completed calls")
        expected_peak = config.max_concurrent * len(runtimes)
        if peak_active != expected_peak:
            failures.append(f"peak active {peak_active} did not reach {expected_peak}")
        if active_at_end or leaked_admission:
            failures.append(
                f"active calls leaked: counters={active_at_end}, admission={leaked_admission}"
            )
        if heap_growth > config.heap_growth_limit_bytes:
            failures.append(
                f"heap growth {heap_growth} exceeded {config.heap_growth_limit_bytes} bytes"
            )
        if rss_growth is None:
            failures.append("resident-memory high-water mark is unavailable on this platform")
        elif rss_growth > config.rss_growth_limit_bytes:
            failures.append(
                f"RSS growth {rss_growth} exceeded {config.rss_growth_limit_bytes} bytes"
            )
        if fd_growth is None:
            failures.append("file-descriptor count is unavailable on this platform")
        elif fd_growth > config.fd_growth_limit:
            failures.append(f"file-descriptor growth {fd_growth} exceeded {config.fd_growth_limit}")

        return SoakReport(
            duration_s=round(elapsed, 3),
            runtimes=runtimes,
            max_concurrent=config.max_concurrent,
            calls_started=calls_started,
            calls_completed=calls_completed,
            terminal_events_verified=terminal_verified,
            peak_active=peak_active,
            active_at_end=active_at_end,
            heap_growth_bytes=heap_growth,
            peak_heap_growth_bytes=peak_heap_growth,
            rss_growth_bytes=rss_growth,
            fd_growth=fd_growth,
            failures=tuple(failures),
        )
    finally:
        if not tracing_was_active:
            tracemalloc.stop()


async def _one_soak_call(
    repository: SQLiteRepository,
    manager: PipecatLifecycleManager,
    admission: AdmissionController,
    *,
    runtime: SoakRuntime,
    hold_s: float,
    sequence: int,
) -> None:
    call_id = f"call_soak_{runtime}_{uuid.uuid4().hex}"
    reservation = await admission.acquire(call_id)
    lifecycle = await manager.begin(
        _soak_agent(runtime),
        PipecatCall(
            call_id=call_id,
            channel="web",
            direction="inbound",
            provider=runtime,
        ),
        reservation,
    )
    lifecycle.buffer.data["sequence"] = sequence
    lifecycle.buffer.outcome = "soak_completed"
    await repository.append_timeline(
        call_id,
        TimelineEvent(
            event_type="soak.simulated_turn",
            details={"runtime": runtime, "sequence": sequence},
        ),
    )
    await repository.append_transcript(
        call_id,
        TranscriptTurn(
            turn_id=f"user-{sequence}",
            role="user",
            text="Run the deterministic soak turn.",
            t_ms=1,
        ),
    )
    await repository.append_transcript(
        call_id,
        TranscriptTurn(
            turn_id=f"assistant-{sequence}",
            role="assistant",
            text="The deterministic soak turn completed.",
            t_ms=2,
        ),
    )
    await asyncio.sleep(hold_s)
    terminal = await lifecycle.finish("agent_hangup", provider_state="completed")
    observed = await repository.get_terminal_event_for_call(call_id)
    if observed.event_id != terminal.event_id:
        raise VoicekitError("VK-TST-005", detail=f"{call_id} terminal event changed.")


def _soak_agent(runtime: SoakRuntime) -> Agent:
    return Agent(
        name=f"soak-{runtime}",
        runtime=runtime,
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Exercise the deterministic lifecycle soak.",
        flow="flow:entry",
        tools="tools",
        web=Web(enabled=True, allowed_origins=["http://localhost:5173"]),
        results=Results(
            webhook="https://soak.invalid/results",
            secret_env="VOICEKIT_WEBHOOK_SECRET",  # pragma: allowlist secret
        ),
    )


def _fd_count() -> int | None:
    for candidate in (Path("/proc/self/fd"), Path("/dev/fd")):
        try:
            return len(tuple(candidate.iterdir()))
        except OSError:
            continue
    return None


class _Usage(Protocol):
    ru_maxrss: int | float


class _ResourceModule(Protocol):
    RUSAGE_SELF: int
    getrusage: Callable[[int], _Usage]


def _maximum_rss_bytes() -> int | None:
    try:
        resource = cast("_ResourceModule", importlib.import_module("resource"))
    except ImportError:
        return None
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # Linux reports KiB; Darwin and the BSDs report bytes.
    return value * 1024 if sys.platform.startswith("linux") else value


def _prepare_database(path: Path) -> Path:
    database = path.expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    return database
