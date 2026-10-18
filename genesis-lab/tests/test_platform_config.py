"""Host/container URL resolution (Linux/WSL custom broker URL)."""

from types import SimpleNamespace

from supervisor.cli import _broker_url_for_containers, _container_host_base


def test_default_container_broker_url():
    settings = SimpleNamespace(broker_url="", broker_port=8787)
    assert _broker_url_for_containers(settings) == "http://host.docker.internal:8787"
    assert _container_host_base(settings) == "http://host.docker.internal"


def test_override_is_used_for_broker_and_mock():
    settings = SimpleNamespace(broker_url="http://172.20.10.2:8787", broker_port=8787)
    assert _broker_url_for_containers(settings) == "http://172.20.10.2:8787"
    assert _container_host_base(settings) == "http://172.20.10.2"


def test_ipv6_override_is_bracketed():
    settings = SimpleNamespace(broker_url="http://[fd00::1]:8787", broker_port=8787)
    assert _container_host_base(settings) == "http://[fd00::1]"
