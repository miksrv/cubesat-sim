"""Test configuration.

The environment is set up **before** anything from ``cubesat`` is imported,
because ``cubesat.common.config`` resolves its paths at import time. Every test
therefore runs against a temporary data directory, the repository's own config
files, and the mock HAL — never a real bus, a real broker or a system path.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_TMP = Path(tempfile.mkdtemp(prefix="cubesat-test-"))

os.environ.setdefault("CUBESAT_CONFIG_DIR", str(REPO_ROOT / "config"))
os.environ.setdefault("CUBESAT_DATA_DIR", str(_TMP / "data"))
os.environ.setdefault("CUBESAT_RUN_DIR", str(_TMP / "run"))
os.environ.setdefault("CUBESAT_LOG_DIR", str(_TMP / "log"))
os.environ.setdefault("CUBESAT_MOCK_HARDWARE", "1")
for _key in ("CUBESAT_DATA_DIR", "CUBESAT_RUN_DIR", "CUBESAT_LOG_DIR"):
    Path(os.environ[_key]).mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402

from tests.fakes.mqtt import FakeMqttClient  # noqa: E402


@pytest.fixture
def fake_client() -> FakeMqttClient:
    return FakeMqttClient()


@pytest.fixture
def service_factory(monkeypatch):
    """Build a Service subclass wired to a fake broker.

    Returns ``(service, client)``. Nothing is connected to anything;
    ``client.deliver(...)`` is how a test simulates an inbound message.
    """

    def build(cls, **kwargs):
        from cubesat.common import mqtt as mqtt_factory

        client = FakeMqttClient()
        monkeypatch.setattr(mqtt_factory, "make_client", lambda _name: client)
        monkeypatch.setattr(mqtt_factory, "connect", lambda _client: None)
        service = cls(**kwargs)
        client.bind(service)
        return service, client

    return build
