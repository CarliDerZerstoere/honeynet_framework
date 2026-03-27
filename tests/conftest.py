"""
Shared test fixtures for the honeynet framework test suite.

Provides commonly used WorldModel instances, projections, and helpers
so individual test files don't need to duplicate fixture setup.
"""

import pytest
from pathlib import Path

from honeynet_framework.models import (
    Organization,
    System,
    SystemDeploy,
    SystemKind,
    SystemSimulate,
    VolumeMount,
    WorldModel,
    WorldZone,
    ZoneDeploy,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def make_test_world_model() -> WorldModel:
    """Create a minimal test WorldModel with 2 zones and 2 systems."""
    return WorldModel(
        organization=Organization(name="Test Corp"),
        zones={
            "dmz": WorldZone(name="dmz", deploy=ZoneDeploy(network_name="dmz", internal=False)),
            "internal": WorldZone(name="internal", deploy=ZoneDeploy(network_name="internal", internal=True)),
        },
        systems={
            "web": System(
                name="web",
                kind=SystemKind.WEB,
                deploy=SystemDeploy(
                    image="nginx:1.25-alpine",
                    zone="dmz",
                    ports=[80],
                    env=["NGINX_HOST=test.local"],
                ),
                simulate=SystemSimulate(hostname="web01.test.local", role="Web Server"),
            ),
            "db": System(
                name="db",
                kind=SystemKind.DATABASE,
                deploy=SystemDeploy(
                    image="postgres:16-alpine",
                    zone="internal",
                    ports=[5432],
                    env=["POSTGRES_PASSWORD=admin123"],
                    depends_on=["web"],
                ),
            ),
        },
    )


@pytest.fixture
def test_world_model() -> WorldModel:
    """Pytest fixture providing a minimal test WorldModel."""
    return make_test_world_model()
