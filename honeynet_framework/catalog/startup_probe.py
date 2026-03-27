"""
Startup probe for Docker containers.

Runs a container briefly, polls ``docker inspect`` to determine whether it
stays alive long enough to be considered a daemon, and collects startup logs
for the LLM repair loop when it fails.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class StartupProbeResult:
    success: bool
    running: bool
    logs: str = ""
    error: str = ""


def probe_startup_profile(
    image_ref: str,
    command: list[str],
    env: dict[str, str],
    timeout_seconds: int = 30,
) -> StartupProbeResult:
    """Run *image_ref* briefly and check whether it stays alive.

    Two-phase adaptive probe:

    * **Phase 1 (0-3 s)** — quick exit detection.  If the container exits
      with a non-zero code within the first few seconds the failure is
      immediate (wrong command, missing env, fictional script).
    * **Phase 2 (3-timeout s)** — alive check.  If ``docker inspect``
      shows ``Running: true`` at *any* poll the container's PID 1 is up
      and the probe succeeds.  Slow-starting images (postgres, opensearch,
      keycloak, vault) simply need more time to initialise — that is not
      a probe failure.  Real health verification happens post-deployment.

    Parameters
    ----------
    image_ref:
        Docker image to test (e.g. ``"python:3.12-slim"``).
    command:
        Entrypoint override to pass to ``docker run``.
    env:
        Environment variables to pass via ``-e KEY=VALUE``.
    timeout_seconds:
        Wall-clock budget for the entire probe (default 30 s).

    Returns
    -------
    StartupProbeResult
        ``success=True`` when the container is confirmed running.
        On failure ``logs`` contains up to 80 lines of container output
        for the repair prompt.
    """
    container_name = f"hn-startup-probe-{uuid.uuid4().hex[:12]}"
    # Phase 1 duration: detect immediate crashes
    quick_exit_window = 3.0
    run_cmd = ["docker", "run", "-d", "--name", container_name]
    for key, value in sorted(env.items()):
        run_cmd.extend(["-e", f"{key}={value}"])
    run_cmd.append(image_ref)
    run_cmd.extend(command)

    try:
        started = subprocess.run(
            run_cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if started.returncode != 0:
            return StartupProbeResult(
                success=False,
                running=False,
                error=(started.stderr or started.stdout or "").strip(),
            )

        started_at = time.time()
        deadline = started_at + float(timeout_seconds)
        running = False
        last_status = ""
        first_running_at: float | None = None
        while time.time() < deadline:
            inspect = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{json .State}}",
                    container_name,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if inspect.returncode != 0 or not inspect.stdout.strip():
                break
            try:
                state = json.loads(inspect.stdout.strip())
            except json.JSONDecodeError:
                break
            running = bool(state.get("Running"))
            status = str(state.get("Status") or "")
            last_status = status

            if running:
                if first_running_at is None:
                    first_running_at = time.time()
                uptime = time.time() - first_running_at
                if uptime >= quick_exit_window:
                    # Container survived the quick-exit window since it
                    # first became running — PID 1 is alive, probe passes.
                    return StartupProbeResult(success=True, running=True)
                # Still in phase 1 — poll quickly to catch immediate exits
                time.sleep(0.75)
                continue

            # Container stopped running — reset the uptime tracker so
            # restart loops don't accumulate disconnected running windows.
            first_running_at = None

            if status in {"exited", "dead"}:
                # Container crashed — real failure
                break
            time.sleep(1.5)

        logs = subprocess.run(
            ["docker", "logs", "--tail", "80", container_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return StartupProbeResult(
            success=False,
            running=running,
            logs=((logs.stdout or "") + (logs.stderr or "")).strip(),
            error=last_status,
        )
    except Exception as exc:
        logger.debug("startup probe failed for %s: %s", image_ref, exc)
        return StartupProbeResult(success=False, running=False, error=str(exc))
    finally:
        try:
            rm = subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                text=True,
                timeout=max(2, timeout_seconds),
            )
            if rm.returncode != 0:
                logger.warning(
                    "startup probe: failed to remove container %s (rc=%d): %s",
                    container_name, rm.returncode, (rm.stderr or rm.stdout or "").strip()[:200],
                )
        except Exception as rm_exc:
            logger.warning("startup probe: could not remove container %s: %s", container_name, rm_exc)
