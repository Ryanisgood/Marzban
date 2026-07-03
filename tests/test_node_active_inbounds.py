import os
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("XRAY_EXECUTABLE_PATH", "/bin/echo")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.crud import get_node_by_id
from app.db.models import Node as DBNode, Proxy, ProxyInbound, User as DBUser
from app.models.node import NodeInboundsMode
from app.models.proxy import ProxyTypes
from app.models.user import UserDataLimitResetStrategy, UserStatus
from app.routers.node import validate_inbounds_selection
from app.xray import operations
from app.xray.config import XRayConfig
from app.xray.node_status import build_node_runtime_status
from app.xray.node import NodeAPIError, ReSTXRayNode


def _config():
    return XRayConfig(
        {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {
                    "tag": "HY2",
                    "protocol": "hysteria",
                    "port": 8443,
                    "settings": {"version": 2, "users": []},
                    "streamSettings": {"network": "hysteria"},
                },
                {"tag": "VLESS", "protocol": "vless", "port": 443},
            ],
            "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
        }
    )


def _same_tcp_port_config():
    return XRayConfig(
        {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {"tag": "VLESS", "protocol": "vless", "port": 443},
                {"tag": "VMESS", "protocol": "vmess", "port": 443},
            ],
            "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
        }
    )


def _hy2_vmess_config():
    return XRayConfig(
        {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {
                    "tag": "HY2",
                    "protocol": "hysteria",
                    "port": 8443,
                    "settings": {"version": 2, "users": []},
                    "streamSettings": {"network": "hysteria"},
                },
                {"tag": "VMESS", "protocol": "vmess", "port": 443},
            ],
            "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
        }
    )


def _wildcard_tcp_port_config():
    return XRayConfig(
        {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {"tag": "PUBLIC", "listen": "0.0.0.0", "protocol": "vless", "port": 443},
                {"tag": "LOCAL", "listen": "127.0.0.1", "protocol": "vmess", "port": 443},
            ],
            "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
        }
    )


def test_rest_node_filters_config_to_active_inbounds():
    node = ReSTXRayNode.__new__(ReSTXRayNode)
    node.active_inbounds = ["VLESS"]

    prepared = node._prepare_config(_config())

    tags = [inbound["tag"] for inbound in prepared["inbounds"]]
    assert "VLESS" in tags
    assert "HY2" not in tags


def test_rest_node_sends_active_inbounds_to_rust_node(monkeypatch):
    node = ReSTXRayNode.__new__(ReSTXRayNode)
    node.active_inbounds = ["VLESS"]
    node._session_id = "session-id"

    captured = {}

    def fake_make_request(path, timeout, **params):
        captured.update(params)
        return {"xray_api": True, "features": ["controller_inbounds"]}

    monkeypatch.setattr(node, "make_request", fake_make_request)
    monkeypatch.setattr(node, "_configure_xray_api", lambda response: None)

    node.restart(_config())

    assert captured["inbounds"] == ["VLESS"]
    assert '"tag": "VLESS"' in captured["config"]
    assert '"tag": "HY2"' not in captured["config"]


def test_rest_node_caches_runtime_diagnostics_from_rust_node(monkeypatch):
    node = ReSTXRayNode.__new__(ReSTXRayNode)
    node.active_inbounds = ["HY2"]
    node._session_id = "session-id"

    response = {
        "xray_api": False,
        "features": ["controller_inbounds", "core_kind", "node_diagnostics"],
        "core_kind": "sing-box",
        "node_version": "0.1.0",
        "installed_cores": {
            "xray": {"installed": True, "version": "26.6.27", "path": "/usr/local/bin/xray"},
            "sing-box": {"installed": True, "version": "1.13.0", "path": "/usr/local/bin/sing-box"},
        },
        "memory": {"agent_rss_bytes": 1024, "core_rss_bytes": 2048},
        "local_listening_ports": [{"transport": "udp", "port": 8443}],
        "configured_inbound_ports": [{"tag": "HY2", "transport": "udp", "port": 8443}],
        "last_core_restart_at": 1783030000,
    }

    monkeypatch.setattr(node, "make_request", lambda *args, **kwargs: response)
    monkeypatch.setattr(node, "_configure_xray_api", lambda response: node._update_runtime_state(response))

    node.restart(_config())

    assert node.node_version == "0.1.0"
    assert node.installed_cores["sing-box"]["version"] == "1.13.0"
    assert node.memory["core_rss_bytes"] == 2048
    assert node.local_listening_ports == [{"transport": "udp", "port": 8443}]
    assert node.configured_inbound_ports == [{"tag": "HY2", "transport": "udp", "port": 8443}]
    assert node.last_core_restart_at == 1783030000


def test_rest_node_rejects_panel_mode_when_node_lacks_controller_inbounds_feature(monkeypatch):
    node = ReSTXRayNode.__new__(ReSTXRayNode)
    node.active_inbounds = ["VLESS"]
    node._session_id = "session-id"

    paths = []

    def fake_make_request(path, timeout, **params):
        paths.append(path)
        return {"xray_api": True}

    monkeypatch.setattr(node, "make_request", fake_make_request)

    with pytest.raises(NodeAPIError) as exc_info:
        node.restart(_config())
    assert "controller-managed inbounds" in exc_info.value.detail
    assert paths == ["/ping", "/"]
    assert "/restart" not in paths


def test_rest_node_omits_active_inbounds_in_legacy_mode(monkeypatch):
    node = ReSTXRayNode.__new__(ReSTXRayNode)
    node.active_inbounds = None
    node._session_id = "session-id"

    captured = {}

    def fake_make_request(path, timeout, **params):
        captured.update(params)
        return {"xray_api": True}

    monkeypatch.setattr(node, "make_request", fake_make_request)
    monkeypatch.setattr(node, "_configure_xray_api", lambda response: None)

    node.restart(_config())

    assert "inbounds" not in captured
    assert '"tag": "HY2"' in captured["config"]
    assert '"tag": "VLESS"' in captured["config"]


def test_node_active_inbounds_helper_uses_panel_mode_only():
    panel_node = SimpleNamespace(
        inbounds_mode=NodeInboundsMode.panel,
        active_inbounds=["VLESS"],
    )
    legacy_node = SimpleNamespace(
        inbounds_mode=NodeInboundsMode.legacy,
        active_inbounds=["HY2"],
    )

    assert operations._node_active_inbounds(panel_node) == ["VLESS"]
    assert operations._node_active_inbounds(legacy_node) is None


def test_node_runtime_status_describes_sing_box_strategy(monkeypatch):
    config = _config()
    monkeypatch.setattr(operations.xray, "config", config)
    monkeypatch.setattr(
        operations.xray,
        "hosts",
        {"HY2": [{"address": ["203.0.113.10"], "port": 9443}]},
    )

    dbnode = SimpleNamespace(
        id=1,
        address="203.0.113.10",
        inbounds_mode=NodeInboundsMode.panel,
        active_inbounds=["HY2"],
    )
    runtime_node = SimpleNamespace(
        last_started_inbounds=["VLESS"],
        core_kind="xray",
        xray_api_available=False,
        node_version="0.1.0",
        installed_cores={"sing-box": {"installed": True, "version": "1.13.0"}},
        memory={"agent_rss_bytes": 1024, "core_rss_bytes": 2048},
        local_listening_ports=[{"transport": "udp", "port": 9443}],
        configured_inbound_ports=[{"tag": "HY2", "transport": "udp", "port": 8443}],
        last_core_restart_at=1783030000,
    )

    status = build_node_runtime_status(
        dbnode,
        runtime_node=runtime_node,
        inbound_user_counts={"HY2": 10},
    )

    assert status.expected_core == "sing-box"
    assert status.actual_core == "xray"
    assert status.core_reason == "INBOUNDS contains hysteria2: HY2"
    assert status.xray_api_available is False
    assert status.restart_required is True
    assert status.active_inbounds_details[0].tag == "HY2"
    assert status.active_inbounds_details[0].port == 8443
    assert status.active_inbounds_details[0].public_port == 9443
    assert status.active_inbounds_details[0].users_count == 10
    assert status.node_version == "0.1.0"
    assert status.installed_cores["sing-box"]["version"] == "1.13.0"
    assert status.memory["core_rss_bytes"] == 2048
    assert status.local_listening_ports[0]["port"] == 9443
    assert status.configured_inbound_ports[0]["tag"] == "HY2"
    assert status.last_core_restart_at == 1783030000


def test_node_runtime_status_describes_xray_strategy(monkeypatch):
    config = _config()
    monkeypatch.setattr(operations.xray, "config", config)
    monkeypatch.setattr(operations.xray, "hosts", {})

    dbnode = SimpleNamespace(
        id=2,
        address="198.51.100.20",
        inbounds_mode=NodeInboundsMode.panel,
        active_inbounds=["VLESS"],
    )
    runtime_node = SimpleNamespace(
        last_started_inbounds=["VLESS"],
        core_kind="xray",
        xray_api_available=True,
    )

    status = build_node_runtime_status(
        dbnode,
        runtime_node=runtime_node,
        inbound_user_counts={"VLESS": 3},
    )

    assert status.expected_core == "xray"
    assert status.actual_core == "xray"
    assert status.core_reason == "All active inbounds are Xray-compatible"
    assert status.xray_api_available is True
    assert status.restart_required is False
    assert status.active_inbounds_details[0].public_port == 443
    assert status.active_inbounds_details[0].users_count == 3


def test_node_runtime_status_requires_restart_when_expected_core_is_not_running(monkeypatch):
    config = _config()
    monkeypatch.setattr(operations.xray, "config", config)
    monkeypatch.setattr(operations.xray, "hosts", {})

    dbnode = SimpleNamespace(
        id=2,
        address="198.51.100.20",
        inbounds_mode=NodeInboundsMode.panel,
        active_inbounds=["VLESS"],
    )
    runtime_node = SimpleNamespace(
        last_started_inbounds=["VLESS"],
        core_kind=None,
        xray_api_available=False,
    )

    status = build_node_runtime_status(dbnode, runtime_node=runtime_node)

    assert status.actual_core is None
    assert status.restart_required is True


def test_node_runtime_status_requires_restart_when_runtime_node_is_missing(monkeypatch):
    config = _config()
    monkeypatch.setattr(operations.xray, "config", config)
    monkeypatch.setattr(operations.xray, "hosts", {})

    dbnode = SimpleNamespace(
        id=2,
        address="198.51.100.20",
        inbounds_mode=NodeInboundsMode.panel,
        active_inbounds=["VLESS"],
    )

    status = build_node_runtime_status(dbnode, runtime_node=None)

    assert status.expected_core == "xray"
    assert status.actual_core is None
    assert status.restart_required is True


def test_node_runtime_status_keeps_stale_active_inbound_visible(monkeypatch):
    config = _config()
    monkeypatch.setattr(operations.xray, "config", config)

    dbnode = SimpleNamespace(
        id=2,
        address="198.51.100.20",
        inbounds_mode=NodeInboundsMode.panel,
        active_inbounds=["REMOVED"],
    )

    status = build_node_runtime_status(dbnode, runtime_node=None)

    assert status.expected_core is None
    assert status.core_reason == "Unknown active inbound(s): REMOVED"
    assert status.active_inbounds_details[0].tag == "REMOVED"
    assert status.active_inbounds_details[0].protocol == "unknown"


def test_node_runtime_status_marks_legacy_mode_unknown(monkeypatch):
    config = _config()
    monkeypatch.setattr(operations.xray, "config", config)

    dbnode = SimpleNamespace(
        id=3,
        address="192.0.2.30",
        inbounds_mode=NodeInboundsMode.legacy,
        active_inbounds=["HY2"],
    )

    status = build_node_runtime_status(dbnode, runtime_node=None)

    assert status.expected_core is None
    assert status.actual_core is None
    assert status.core_reason == "Legacy INBOUNDS mode; controller does not own this node's inbound selection"
    assert status.restart_required is False
    assert status.active_inbounds_details == []


def test_node_inbounds_validation_rejects_missing_host(monkeypatch):
    config = _config()
    monkeypatch.setattr(operations.xray, "config", config)
    monkeypatch.setattr(operations.xray, "hosts", {})

    with pytest.raises(Exception) as exc_info:
        validate_inbounds_selection(NodeInboundsMode.panel, ["HY2"])

    assert getattr(exc_info.value, "status_code") == 400
    assert "host" in exc_info.value.detail.lower()
    assert "HY2" in exc_info.value.detail


def test_node_inbounds_validation_rejects_same_transport_port_conflict(monkeypatch):
    config = _same_tcp_port_config()
    monkeypatch.setattr(operations.xray, "config", config)
    monkeypatch.setattr(
        operations.xray,
        "hosts",
        {"VLESS": [{"address": "example.com"}], "VMESS": [{"address": "example.com"}]},
    )

    with pytest.raises(Exception) as exc_info:
        validate_inbounds_selection(NodeInboundsMode.panel, ["VLESS", "VMESS"])

    assert getattr(exc_info.value, "status_code") == 400
    assert "port" in exc_info.value.detail.lower()
    assert "443" in exc_info.value.detail


def test_node_inbounds_validation_rejects_wildcard_bind_port_conflict(monkeypatch):
    config = _wildcard_tcp_port_config()
    monkeypatch.setattr(operations.xray, "config", config)
    monkeypatch.setattr(
        operations.xray,
        "hosts",
        {"PUBLIC": [{"address": "example.com"}], "LOCAL": [{"address": "example.com"}]},
    )

    with pytest.raises(Exception) as exc_info:
        validate_inbounds_selection(NodeInboundsMode.panel, ["PUBLIC", "LOCAL"])

    assert getattr(exc_info.value, "status_code") == 400
    assert "port" in exc_info.value.detail.lower()
    assert "443" in exc_info.value.detail


def test_node_inbounds_validation_allows_udp_and_tcp_on_same_port(monkeypatch):
    config = _config()
    monkeypatch.setattr(operations.xray, "config", config)
    monkeypatch.setattr(
        operations.xray,
        "hosts",
        {"HY2": [{"address": "example.com"}], "VLESS": [{"address": "example.com"}]},
    )

    warnings = validate_inbounds_selection(
        NodeInboundsMode.panel,
        ["HY2", "VLESS"],
        runtime_node=SimpleNamespace(
            installed_cores={"sing-box": {"installed": True}},
        ),
    )

    assert any("reachability" in warning.lower() for warning in warnings)


def test_node_inbounds_validation_rejects_missing_required_core(monkeypatch):
    config = _config()
    monkeypatch.setattr(operations.xray, "config", config)
    monkeypatch.setattr(operations.xray, "hosts", {"HY2": [{"address": "example.com"}]})

    with pytest.raises(Exception) as exc_info:
        validate_inbounds_selection(
            NodeInboundsMode.panel,
            ["HY2"],
            runtime_node=SimpleNamespace(
                installed_cores={"sing-box": {"installed": False}},
            ),
        )

    assert getattr(exc_info.value, "status_code") == 400
    assert "sing-box" in exc_info.value.detail


def test_node_inbounds_validation_rejects_sing_box_unsupported_protocol(monkeypatch):
    config = _hy2_vmess_config()
    monkeypatch.setattr(operations.xray, "config", config)
    monkeypatch.setattr(
        operations.xray,
        "hosts",
        {"HY2": [{"address": "example.com"}], "VMESS": [{"address": "example.com"}]},
    )

    with pytest.raises(Exception) as exc_info:
        validate_inbounds_selection(
            NodeInboundsMode.panel,
            ["HY2", "VMESS"],
            runtime_node=SimpleNamespace(
                installed_cores={"sing-box": {"installed": True}},
            ),
        )

    assert getattr(exc_info.value, "status_code") == 400
    assert "sing-box" in exc_info.value.detail
    assert "VMESS" in exc_info.value.detail


def test_node_api_iterator_skips_inbounds_not_running_on_panel_node():
    class Node:
        connected = True
        started = True
        active_inbounds = ["VLESS"]

        @property
        def api(self):
            raise AssertionError("node api should not be accessed")

    assert list(operations._node_apis_for_inbound([Node()], "HY2")) == []


def test_node_api_iterator_skips_nodes_without_xray_api():
    class Node:
        connected = True
        started = True
        active_inbounds = ["HY2"]

        @property
        def api(self):
            raise ConnectionError("Node core does not expose Xray API")

    assert list(operations._node_apis_for_inbound([Node()], "HY2")) == []


def test_get_node_by_id_eager_loads_active_inbounds_after_session_close():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    with TestingSession() as db:
        inbound = ProxyInbound(tag="VLESS")
        dbnode = DBNode(
            name="node-1",
            address="127.0.0.1",
            port=62050,
            api_port=62051,
            inbounds_mode=NodeInboundsMode.panel,
            active_inbound_objects=[inbound],
        )
        db.add_all([inbound, dbnode])
        db.commit()
        node_id = dbnode.id

    with TestingSession() as db:
        loaded = get_node_by_id(db, node_id)

    assert loaded.active_inbounds == ["VLESS"]


def test_update_user_restarts_nodes_when_hysteria_proxy_was_removed(monkeypatch):
    config = _config()
    monkeypatch.setattr(operations.xray, "config", config)
    monkeypatch.setattr(operations.xray, "nodes", {})
    reloads = []
    monkeypatch.setattr(
        operations,
        "_restart_started_nodes_for_config_reload",
        lambda inbound_tags=None: reloads.append(set(inbound_tags or [])),
    )
    monkeypatch.setattr(operations, "_alter_inbound_user", lambda *args, **kwargs: None)
    monkeypatch.setattr(operations, "_remove_user_from_inbound", lambda *args, **kwargs: None)

    dbuser = DBUser(
        id=7,
        username="alice",
        status=UserStatus.active,
        used_traffic=0,
        data_limit_reset_strategy=UserDataLimitResetStrategy.no_reset,
        created_at=datetime.utcnow(),
        proxies=[
            Proxy(
                type=ProxyTypes.VLESS,
                settings={"id": "11111111-1111-1111-1111-111111111111"},
            )
        ],
    )
    dbuser.links = ["vless://placeholder"]
    dbuser.subscription_url = "https://example.test/sub"

    operations.update_user(dbuser)

    assert reloads == [{"HY2"}]
