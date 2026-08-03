from __future__ import annotations

from pathlib import Path

import pytest

from voicey.errors import VoiceyError
from voicey.testing import SoakConfig, SoakReport, run_engine_soak
from voicey.testing import soak as soak_module


@pytest.mark.asyncio
async def test_dual_runtime_soak_reaches_capacity_and_leaks_nothing(tmp_path: Path) -> None:
    report = await run_engine_soak(
        tmp_path / "soak.sqlite3",
        SoakConfig(
            duration_s=0.35,
            max_concurrent=3,
            call_hold_s=0.02,
            heap_growth_limit_bytes=32 * 1024 * 1024,
            rss_growth_limit_bytes=64 * 1024 * 1024,
            fd_growth_limit=4,
        ),
    )

    report.assert_healthy()
    assert report.calls_started == report.calls_completed
    assert report.calls_completed == report.terminal_events_verified
    assert report.peak_active == 6
    assert report.active_at_end == 0


def test_soak_config_fails_closed() -> None:
    with pytest.raises(VoiceyError) as invalid:
        SoakConfig(duration_s=0, max_concurrent=1)
    assert invalid.value.code == "VY-TST-005"


@pytest.mark.asyncio
async def test_soak_rejects_empty_or_duplicate_runtime_selection(tmp_path: Path) -> None:
    config = SoakConfig(duration_s=0.01, max_concurrent=1, call_hold_s=0.005)
    for runtimes in ((), ("pipecat", "pipecat")):
        with pytest.raises(VoiceyError) as caught:
            await run_engine_soak(
                tmp_path / "unused.sqlite3",
                config,
                runtimes=runtimes,  # type: ignore[arg-type]
            )
        assert caught.value.code == "VY-TST-005"


def test_unhealthy_soak_report_raises_catalog_error() -> None:
    report = SoakReport(
        duration_s=1,
        runtimes=("pipecat",),
        max_concurrent=1,
        calls_started=1,
        calls_completed=0,
        terminal_events_verified=0,
        peak_active=1,
        active_at_end=1,
        heap_growth_bytes=0,
        peak_heap_growth_bytes=0,
        rss_growth_bytes=None,
        fd_growth=None,
        failures=("one call leaked",),
    )

    assert not report.healthy
    with pytest.raises(VoiceyError) as caught:
        report.assert_healthy()
    assert caught.value.code == "VY-TST-005"


@pytest.mark.asyncio
async def test_soak_resource_bounds_and_unavailable_metrics_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rss_values = iter([0, 65 * 1024 * 1024])
    fd_values = iter([3, 8])
    monkeypatch.setattr(soak_module, "_maximum_rss_bytes", lambda: next(rss_values))
    monkeypatch.setattr(soak_module, "_fd_count", lambda: next(fd_values))
    bounded = await run_engine_soak(
        tmp_path / "bounded.sqlite3",
        SoakConfig(
            duration_s=0.03,
            max_concurrent=1,
            call_hold_s=0.005,
            heap_growth_limit_bytes=0,
            rss_growth_limit_bytes=64 * 1024 * 1024,
            fd_growth_limit=4,
        ),
        runtimes=("pipecat",),
    )
    assert any("heap growth" in failure for failure in bounded.failures)
    assert any("RSS growth" in failure for failure in bounded.failures)
    assert any("file-descriptor growth" in failure for failure in bounded.failures)

    monkeypatch.setattr(soak_module, "_maximum_rss_bytes", lambda: None)
    monkeypatch.setattr(soak_module, "_fd_count", lambda: None)
    unavailable = await run_engine_soak(
        tmp_path / "unavailable.sqlite3",
        SoakConfig(duration_s=0.03, max_concurrent=1, call_hold_s=0.005),
        runtimes=("livekit",),
    )
    assert any("resident-memory" in failure for failure in unavailable.failures)
    assert any("file-descriptor count" in failure for failure in unavailable.failures)
