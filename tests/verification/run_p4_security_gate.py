"""Run the complete local P4.6 signature, secret, dependency, and image audit."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "docker-project"
_ENV_SENTINEL = "vk-security-gate-runtime-only-value-7d7f3f2e"


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    status: str
    command: str
    duration_s: float
    detail: str


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(".voicekit/verification/p4-security-report.json"),
    )
    args = parser.parse_args()
    wheel = args.wheel.expanduser().resolve()
    if not wheel.is_file():
        parser.error("--wheel must point to a built wheel")
    report_path = args.report.expanduser().resolve()
    pytest = [sys.executable, "-m", "pytest", "-q", "--no-cov"]
    results = [
        _run(
            "signature_negative_matrix",
            [
                *pytest,
                "tests/unit/test_results_signing.py",
                (
                    "tests/certification/test_twilio_adapter.py::"
                    "test_http_and_websocket_signatures_use_exact_trusted_public_url"
                ),
                (
                    "tests/certification/test_twilio_adapter.py::"
                    "test_callback_variants_request_rejection_and_call_update_failure"
                ),
                (
                    "tests/certification/test_telnyx_adapter.py::"
                    "test_ed25519_verification_rejects_tamper_replay_future_and_websocket"
                ),
                (
                    "tests/certification/test_vobiz_adapter.py::"
                    "test_v3_v2_signature_canonicalization_replay_and_negative_cases"
                ),
                (
                    "tests/certification/test_plivo_adapter.py::"
                    "test_installed_sdk_v3_signature_form_canonicalization_replay_and_negatives"
                ),
                ("tests/unit/test_relay.py::test_relay_rejects_tampered_signature_and_fence"),
            ],
        ),
        _run(
            "logs_records_and_deploy_secret_boundaries",
            [
                *pytest,
                "tests/unit/test_observability.py",
                "tests/unit/test_security_files.py",
                (
                    "tests/unit/test_deploy_docker.py::"
                    "test_generator_emits_idempotent_secret_free_hardened_artifacts"
                ),
                (
                    "tests/unit/test_deploy_fly.py::"
                    "test_flyctl_runner_maps_discovery_process_failure_and_timeout"
                ),
                (
                    "tests/unit/test_deploy_cloud.py::"
                    "test_platform_runner_maps_missing_failure_and_timeout_without_output_leak"
                ),
                (
                    "tests/unit/test_deploy_railway.py::"
                    "test_railway_cli_runner_maps_missing_process_failure_and_timeout"
                ),
            ],
        ),
        _tracked_secret_scan(),
        _run(
            "python_dependency_audit",
            ["uv", "run", "pip-audit", "--local", "--skip-editable"],
            timeout_s=1200,
        ),
        _run(
            "playground_dependency_audit",
            ["npm", "audit", "--audit-level=high"],
            cwd=ROOT / "playground-web",
            timeout_s=1200,
        ),
        _release_artifact_scan(),
        _container_image_scan(wheel),
    ]
    failures = [result for result in results if result.status != "green"]
    report = {
        "schema_version": 1,
        "phase": "P4.6",
        "gate": "security",
        "status": "failed" if failures else "green",
        "results": [asdict(result) for result in results],
        "truthfulness": (
            "the container row builds and scans the canonical generated production "
            "image; it is not inferred from Dockerfile inspection"
        ),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 1 if failures else 0


def _tracked_secret_scan() -> GateResult:
    started = time.monotonic()
    command = "detect-secrets-hook --baseline .secrets.baseline <tracked-and-untracked-files>"
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            timeout=30,
        )
        files = [
            path.decode("utf-8")
            for path in listed.stdout.split(b"\0")
            if path and not path.startswith(b".voicekit/")
        ]
        completed = subprocess.run(
            ["uv", "run", "detect-secrets-hook", "--baseline", ".secrets.baseline", *files],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        return GateResult(
            name="repository_secret_scan",
            status="failed",
            command=command,
            duration_s=round(time.monotonic() - started, 3),
            detail=f"{type(exc).__name__}: scan did not complete",
        )
    output = completed.stdout.strip() or completed.stderr.strip()
    return GateResult(
        name="repository_secret_scan",
        status="green" if completed.returncode == 0 else "failed",
        command=command,
        duration_s=round(time.monotonic() - started, 3),
        detail=output[-1000:] or f"{len(files)} files scanned against the baseline",
    )


def _release_artifact_scan() -> GateResult:
    started = time.monotonic()
    command = "uv build --wheel --sdist; unpack; trivy fs --scanners secret"
    if shutil.which("trivy") is None:
        return GateResult(
            name="release_artifact_secret_scan",
            status="failed",
            command=command,
            duration_s=0.0,
            detail="trivy is required; install Trivy and rerun the security gate",
        )
    try:
        with tempfile.TemporaryDirectory(prefix="voicekit-security-artifacts-") as directory:
            root = Path(directory)
            dist = root / "dist"
            built = subprocess.run(
                ["uv", "build", "--wheel", "--sdist", "--out-dir", str(dist)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=1200,
            )
            if built.returncode != 0:
                raise RuntimeError(f"uv build exited {built.returncode}")
            extracted = root / "extracted"
            extracted.mkdir()
            archives = [
                path
                for path in sorted(dist.iterdir())
                if path.suffix == ".whl" or path.name.endswith(".tar.gz")
            ]
            if len(archives) != 2:
                raise RuntimeError("uv build did not produce exactly one wheel and one sdist")
            for archive in archives:
                target = extracted / archive.name.replace(".", "-")
                if archive.suffix == ".whl":
                    with zipfile.ZipFile(archive) as wheel:
                        wheel.extractall(target)
                else:
                    shutil.unpack_archive(archive, target)
                unsafe = [
                    path
                    for path in target.rglob("*")
                    if path.is_file() and _unsafe_release_name(path.name)
                ]
                if unsafe:
                    names = ", ".join(str(path.relative_to(target)) for path in unsafe[:10])
                    raise RuntimeError(f"release contains secret-bearing filenames: {names}")
            completed = subprocess.run(
                [
                    "trivy",
                    "fs",
                    "--scanners",
                    "secret",
                    "--exit-code",
                    "1",
                    "--quiet",
                    str(extracted),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=1200,
            )
            output = completed.stdout.strip() or completed.stderr.strip()
    except (OSError, RuntimeError, shutil.ReadError, subprocess.SubprocessError) as exc:
        return GateResult(
            name="release_artifact_secret_scan",
            status="failed",
            command=command,
            duration_s=round(time.monotonic() - started, 3),
            detail=str(exc)[-1000:],
        )
    return GateResult(
        name="release_artifact_secret_scan",
        status="green" if completed.returncode == 0 else "failed",
        command=command,
        duration_s=round(time.monotonic() - started, 3),
        detail=output[-1000:] or "wheel and sdist unpacked; no secret findings",
    )


def _container_image_scan(wheel: Path) -> GateResult:
    started = time.monotonic()
    command = "generate canonical Docker target; docker build; trivy image vuln,secret"
    missing = [executable for executable in ("docker", "trivy") if shutil.which(executable) is None]
    if missing:
        return GateResult(
            name="canonical_container_image_scan",
            status="failed",
            command=command,
            duration_s=0.0,
            detail=f"missing required executable(s): {', '.join(missing)}",
        )
    identity = f"{os.getpid()}-{time.time_ns()}"
    tag = f"voicekit-security-p4:{identity}"
    container = f"voicekit-security-p4-{identity}"
    volume = f"voicekit-security-p4-{identity}"
    detail = ""
    status = "failed"
    try:
        with tempfile.TemporaryDirectory(prefix="voicekit-security-image-") as directory:
            project = Path(directory) / "project"
            shutil.copytree(FIXTURE, project)
            _write_fixture_environment(project / ".env")
            generated = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from voicekit.cli.app import app; app()",
                    "deploy",
                    "docker",
                    "--engine-wheel",
                    str(wheel),
                    "--skip-smoke",
                    "--json",
                ],
                cwd=project,
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if generated.returncode != 0:
                raise RuntimeError(f"Docker artifact generation exited {generated.returncode}")
            built = subprocess.run(
                [
                    "docker",
                    "build",
                    "--pull",
                    "--file",
                    "Dockerfile.voicekit",
                    "--tag",
                    tag,
                    ".",
                ],
                cwd=project,
                check=False,
                capture_output=True,
                text=True,
                timeout=2400,
            )
            if built.returncode != 0:
                output = built.stdout.strip() or built.stderr.strip()
                raise RuntimeError(f"docker build exited {built.returncode}: {output[-1000:]}")
            history = subprocess.run(
                ["docker", "history", "--no-trunc", tag],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            inspected = subprocess.run(
                ["docker", "image", "inspect", tag],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if _ENV_SENTINEL in history.stdout or _ENV_SENTINEL in inspected.stdout:
                raise RuntimeError("runtime-only fixture secret leaked into image metadata")
            _run_image_and_verify_drain(
                tag=tag,
                container=container,
                volume=volume,
                environment_file=project / ".env",
            )
            scanned = subprocess.run(
                [
                    "trivy",
                    "image",
                    "--scanners",
                    "vuln,secret",
                    "--severity",
                    "HIGH,CRITICAL",
                    "--ignore-unfixed",
                    "--exit-code",
                    "1",
                    "--timeout",
                    "20m",
                    "--quiet",
                    tag,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=1500,
            )
            output = scanned.stdout.strip() or scanned.stderr.strip()
            status = "green" if scanned.returncode == 0 else "failed"
            detail = output[-1000:] or (
                "canonical image health and SIGTERM drain passed; no fixed "
                "HIGH/CRITICAL or secret finding"
            )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        detail = str(exc)[-1000:]
    finally:
        subprocess.run(
            ["docker", "container", "rm", "--force", container],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        subprocess.run(
            ["docker", "volume", "rm", "--force", volume],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        subprocess.run(
            ["docker", "image", "rm", "--force", tag],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    return GateResult(
        name="canonical_container_image_scan",
        status=status,
        command=command,
        duration_s=round(time.monotonic() - started, 3),
        detail=detail,
    )


def _run_image_and_verify_drain(
    *,
    tag: str,
    container: str,
    volume: str,
    environment_file: Path,
) -> None:
    subprocess.run(
        ["docker", "volume", "create", volume],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            container,
            "--init",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "512",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=128m",
            "--publish",
            "127.0.0.1::7860",
            "--volume",
            f"{volume}:/app/data",
            "--env-file",
            str(environment_file),
            "--env",
            "VOICEKIT_DEPLOY_TARGET=docker",
            "--env",
            "VOICEKIT_STORAGE_BACKEND=sqlite",
            "--env",
            "VOICEKIT_SQLITE_LOCAL_ONLY=1",
            "--env",
            "VOICEKIT_REPLICA_COUNT=1",
            "--env",
            "VOICEKIT_DATA_DIR=/app/data",
            "--env",
            "VOICEKIT_PORT=7860",
            "--env",
            "VOICEKIT_ADMIN_PORT=7861",
            "--env",
            "VOICEKIT_ADMIN_ORIGIN=http://agent:7861",
            tag,
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    published = subprocess.run(
        ["docker", "port", container, "7860/tcp"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    port = int(published.rsplit(":", maxsplit=1)[-1])
    for attempt in range(60):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health",
                timeout=2,
            ) as response:
                payload = json.loads(response.read())
            if (
                response.status == 200
                and payload.get("ok") is True
                and payload.get("accepting") is True
                and payload.get("storage_ready") is True
            ):
                break
        except (OSError, ValueError, urllib.error.URLError):
            pass
        if attempt == 59:
            logs = subprocess.run(
                ["docker", "logs", container],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = logs.stdout.strip() or logs.stderr.strip()
            raise RuntimeError(f"container did not become healthy: {output[-1000:]}")
        time.sleep(1)
    subprocess.run(
        ["docker", "stop", "--timeout", "20", container],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    exit_code = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.ExitCode}}", container],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    if exit_code != "0":
        raise RuntimeError(f"container SIGTERM drain exited {exit_code}")


def _unsafe_release_name(name: str) -> bool:
    lowered = name.casefold()
    return (
        (lowered.startswith(".env") and lowered != ".env.example")
        or lowered.endswith((".pem", ".p12", ".pfx"))
        or lowered in {"id_rsa", "id_ed25519", "credentials.json", "secrets.json"}
    )


def _write_fixture_environment(path: Path) -> None:
    values: Mapping[str, str] = {
        "ANTHROPIC_API_KEY": _ENV_SENTINEL,
        "CARTESIA_API_KEY": _ENV_SENTINEL,
        "DEEPGRAM_API_KEY": _ENV_SENTINEL,
        "VOICEKIT_INTEGRATOR_SECRET": _ENV_SENTINEL,
        "VOICEKIT_PUBLIC_BASE": "https://voice.example",
        "VOICEKIT_WEBHOOK_SECRET": "whsec_Zml4dHVyZS1zZWNyZXQ=",  # pragma: allowlist secret
    }
    path.write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _run(
    name: str,
    command: Sequence[str],
    *,
    cwd: Path = ROOT,
    timeout_s: float = 600,
) -> GateResult:
    rendered = shlex.join(command)
    print(f"[P4.6 security] {name}: {rendered}", flush=True)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GateResult(
            name=name,
            status="failed",
            command=rendered,
            duration_s=round(time.monotonic() - started, 3),
            detail=f"{type(exc).__name__}: command did not complete",
        )
    output = completed.stdout.strip() or completed.stderr.strip()
    return GateResult(
        name=name,
        status="green" if completed.returncode == 0 else "failed",
        command=rendered,
        duration_s=round(time.monotonic() - started, 3),
        detail=output[-1000:],
    )


if __name__ == "__main__":
    raise SystemExit(main())
