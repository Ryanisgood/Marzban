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
from app.xray import operations
from app.xray.config import XRayConfig
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
        lambda: reloads.append(True),
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

    assert reloads == [True]
