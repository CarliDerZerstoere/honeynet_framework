import asyncio
import json
from pathlib import Path

from honeynet_framework.models import DeployContainer, DeployNetwork, DeployProjection
from honeynet_framework.path_a_diagnostics import (
    _build_resource_to_name_map,
    _extract_failing_resources,
    collect_path_a_apply_failure_diagnostics,
)


class _FailingDeployer:
    async def _run_docker_cmd(self, args):
        raise RuntimeError(f"docker failed: {' '.join(args)}")

    async def _run_tofu(self, args):
        raise RuntimeError(f"tofu failed: {' '.join(args)}")


def test_extract_failing_resources_supports_module_and_indexed_addresses():
    stderr = """
Error: creating container failed
  with module.honeypot[0].docker_container.hn_web__2["main"],
  on main.tofu.json line 1, in resource "docker_container" "hn_web__2":
Error: creating network failed
  with module.net.module.inner.docker_network.dmz__2,
  on main.tofu.json line 2, in resource "docker_network" "dmz__2":
"""
    resources = _extract_failing_resources(stderr)
    assert resources == [
        'module.honeypot[0].docker_container.hn_web__2["main"]',
        "module.net.module.inner.docker_network.dmz__2",
    ]


def test_build_resource_to_name_map_handles_sanitized_and_deduped_ids():
    projection = DeployProjection(
        project_name="test",
        networks=[
            DeployNetwork(name="dmz"),
            DeployNetwork(name="dmz!"),
        ],
        containers=[
            DeployContainer(name="hn_web-app", image="nginx:latest"),
            DeployContainer(name="hn_web app", image="nginx:latest"),
        ],
    )
    mapping = _build_resource_to_name_map(projection)
    assert mapping["docker_network.dmz"] == "dmz"
    assert mapping["docker_network.dmz__2"] == "dmz!"
    assert mapping["docker_container.hn_web_app"] == "hn_web-app"
    assert mapping["docker_container.hn_web_app__2"] == "hn_web app"


def test_collect_path_a_is_best_effort_and_writes_artifact_on_command_exceptions(tmp_path: Path):
    projection = DeployProjection(
        project_name="test",
        networks=[DeployNetwork(name="dmz")],
        containers=[DeployContainer(name="hn_web-app", image="nginx:latest")],
    )
    apply_stderr = (
        "Error: failed\n"
        "with module.honeypot[0].docker_container.hn_web_app,\n"
        "on main.tofu.json line 1\n"
    )

    summary = asyncio.run(
        collect_path_a_apply_failure_diagnostics(
            deployer=_FailingDeployer(),
            projection=projection,
            work_dir=tmp_path,
            apply_stderr=apply_stderr,
            run_id="r-1",
            logs_tail_lines=10,
        )
    )

    assert summary["collected"] is True
    assert summary["docker_targets_count"] == 1
    artifact_path = tmp_path / "path_a_diagnostics.json"
    assert artifact_path.exists()

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["docker_targets"][0]["resource"] == "docker_container.hn_web_app"
    assert artifact["docker_targets"][0]["resource_address"] == "module.honeypot[0].docker_container.hn_web_app"
    assert artifact["docker_targets"][0]["docker_name"] == "hn_web-app"
    assert artifact["docker_inspect"][0]["success"] is False
    assert "docker failed:" in artifact["docker_inspect"][0]["exception"]
    assert artifact["docker_logs_tail"][0]["success"] is False
    assert "docker failed:" in artifact["docker_logs_tail"][0]["exception"]
    assert artifact["tofu_state_list"]["success"] is False
    assert "tofu failed:" in artifact["tofu_state_list"]["exception"]
