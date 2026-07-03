import os
from datetime import datetime, timedelta

os.environ.setdefault("XRAY_EXECUTABLE_PATH", "/bin/echo")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import Node as DBNode, TLS
from app.db.models import ProxyHost
from app.db.crud import create_node_provision_token, redeem_node_provision_token
from app.models.node import NodeInboundsMode
from app.models.node_provision import NodeProvisionCreate, NodeProvisionInbound
from app.models.node_provision import NodeProvisionProtocol
from app.xray.config import XRayConfig
from app.xray.node_provisioning import (
    apply_provisioned_config,
    build_generated_inbounds,
    choose_core_kind,
    hash_install_token,
    provision_node,
    redeem_node_install_payload,
    render_node_install_script,
    verify_install_token,
)


def _db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _db_node(db):
    dbnode = DBNode(
        name="node-1",
        address="203.0.113.10",
        port=62050,
        api_port=62051,
    )
    db.add(dbnode)
    db.commit()
    db.refresh(dbnode)
    return dbnode


def test_choose_core_kind_uses_sing_box_when_hy2_is_selected():
    assert choose_core_kind([NodeProvisionProtocol.hy2]) == "sing-box"
    assert choose_core_kind(
        [
            NodeProvisionProtocol.hy2,
            NodeProvisionProtocol.vless_reality,
            NodeProvisionProtocol.shadowsocks,
        ]
    ) == "sing-box"


def test_choose_core_kind_uses_xray_for_xray_only_protocols():
    assert choose_core_kind([NodeProvisionProtocol.vless_reality]) == "xray"
    assert choose_core_kind([NodeProvisionProtocol.shadowsocks]) == "xray"


def test_install_token_hash_does_not_store_plaintext():
    token = "plain-token"
    token_hash = hash_install_token(token)

    assert token_hash != token
    assert verify_install_token(token, token_hash)
    assert not verify_install_token("other-token", token_hash)


def test_build_generated_inbounds_creates_hy2_vless_and_shadowsocks_templates(monkeypatch):
    import app.xray.node_provisioning as provisioning

    monkeypatch.setattr(
        provisioning,
        "generate_reality_key_pair",
        lambda: ("real-private-key", "real-public-key"),
    )
    inbounds = build_generated_inbounds(
        node_id=42,
        specs=[
            (NodeProvisionProtocol.hy2, 8443),
            (NodeProvisionProtocol.vless_reality, 443),
            (NodeProvisionProtocol.shadowsocks, 8388),
        ],
    )

    assert [item["tag"] for item in inbounds] == [
        "node-42-hy2-8443",
        "node-42-vless-443",
        "node-42-shadowsocks-8388",
    ]
    assert inbounds[0]["protocol"] == "hysteria"
    assert inbounds[0]["settings"]["version"] == 2
    assert inbounds[0]["streamSettings"]["network"] == "hysteria"
    assert inbounds[1]["protocol"] == "vless"
    assert inbounds[1]["settings"]["clients"] == []
    reality_settings = inbounds[1]["streamSettings"]["realitySettings"]
    assert reality_settings["privateKey"] == "real-private-key"
    assert reality_settings["publicKey"] == "real-public-key"
    assert inbounds[2]["protocol"] == "shadowsocks"
    assert inbounds[2]["settings"]["clients"] == []


def test_vless_reality_inbound_generation_rejects_missing_x25519_keys(monkeypatch):
    import app.xray.node_provisioning as provisioning

    monkeypatch.setattr(provisioning, "generate_reality_key_pair", lambda: None)

    try:
        build_generated_inbounds(
            node_id=42,
            specs=[(NodeProvisionProtocol.vless_reality, 443)],
        )
    except ValueError as exc:
        assert "x25519" in str(exc)
    else:
        raise AssertionError("expected VLESS REALITY provisioning to reject missing keys")


def test_generated_inbounds_are_visible_to_xray_config():
    config = {
        "inbounds": build_generated_inbounds(
            node_id=42,
            specs=[(NodeProvisionProtocol.hy2, 8443)],
        ),
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
    }

    parsed = XRayConfig(config)
    assert parsed.inbounds_by_tag["node-42-hy2-8443"]["protocol"] == "hysteria"


def test_create_node_provision_token_stores_hash_not_plaintext():
    db = _db_session()
    dbnode = _db_node(db)

    token, record = create_node_provision_token(
        db,
        node_id=dbnode.id,
        created_by="admin",
        active_inbounds=["node-1-hy2-8443"],
        core_kind="sing-box",
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    )

    assert token
    assert record.token_hash != token
    assert token not in record.token_hash
    assert record.active_inbounds == ["node-1-hy2-8443"]
    assert record.core_kind == "sing-box"


def test_redeem_node_provision_token_is_one_time_use():
    db = _db_session()
    dbnode = _db_node(db)
    token, record = create_node_provision_token(
        db,
        node_id=dbnode.id,
        created_by="admin",
        active_inbounds=["node-1-vless-443"],
        core_kind="xray",
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    )

    redeemed = redeem_node_provision_token(db, token)

    assert redeemed.id == record.id
    assert redeemed.redeemed_at is not None
    assert redeem_node_provision_token(db, token) is None


def test_redeem_node_provision_token_rejects_expired_token():
    db = _db_session()
    dbnode = _db_node(db)
    token, _ = create_node_provision_token(
        db,
        node_id=dbnode.id,
        created_by="admin",
        active_inbounds=["node-1-vless-443"],
        core_kind="xray",
        expires_at=datetime.utcnow() - timedelta(seconds=1),
    )

    assert redeem_node_provision_token(db, token) is None


def test_provision_node_creates_config_hosts_panel_node_and_install_command(monkeypatch):
    import app.xray.node_provisioning as provisioning

    db = _db_session()
    applied_configs = []
    monkeypatch.setattr(
        provisioning,
        "generate_reality_key_pair",
        lambda: ("real-private-key", "real-public-key"),
    )
    payload = NodeProvisionCreate(
        name="rn1c1g",
        address="node.example.com",
        inbounds=[
            NodeProvisionInbound(protocol=NodeProvisionProtocol.hy2, port=8443),
            NodeProvisionInbound(protocol=NodeProvisionProtocol.vless_reality, port=443),
        ],
    )
    current_config = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "existing-ss",
                "listen": "0.0.0.0",
                "port": 1080,
                "protocol": "shadowsocks",
                "settings": {"clients": [], "network": "tcp"},
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
    }

    result = provision_node(
        db,
        payload,
        admin_username="admin",
        controller_url="https://panel.example.com",
        current_config=current_config,
        apply_config=applied_configs.append,
    )

    assert result.node.name == "rn1c1g"
    assert result.node.inbounds_mode == NodeInboundsMode.panel
    assert result.node.active_inbounds == ["node-1-hy2-8443", "node-1-vless-443"]
    assert result.core_kind == "sing-box"
    assert result.install_command.startswith(
        "curl -fsSL https://panel.example.com/api/node/install.sh | sudo bash -s -- --token "
    )
    assert result.install_token
    assert result.install_token in result.install_command

    assert len(applied_configs) == 1
    applied = XRayConfig(applied_configs[0])
    assert "existing-ss" in applied.inbounds_by_tag
    assert "node-1-hy2-8443" in applied.inbounds_by_tag
    assert "node-1-vless-443" in applied.inbounds_by_tag

    hosts = db.query(ProxyHost).order_by(ProxyHost.inbound_tag).all()
    assert [host.inbound_tag for host in hosts] == [
        "node-1-hy2-8443",
        "node-1-vless-443",
    ]
    assert all(host.address == "node.example.com" for host in hosts)
    assert [host.port for host in hosts] == [8443, 443]


def test_apply_provisioned_config_uses_core_config_lifecycle(monkeypatch, tmp_path):
    from app import xray
    import app.xray.node_provisioning as provisioning

    config_path = tmp_path / "xray.json"
    restarts = []
    node_restarts = []
    host_updates = []
    candidate_config = {
        "inbounds": [
            {
                "tag": "node-1-hy2-8443",
                "listen": "0.0.0.0",
                "port": 8443,
                "protocol": "hysteria",
                "settings": {"version": 2, "users": []},
                "streamSettings": {"network": "hysteria"},
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
    }

    class Core:
        def restart(self, startup_config):
            restarts.append(startup_config)

    class Hosts:
        def update(self):
            host_updates.append(True)

    class Node:
        connected = True

    monkeypatch.setattr(provisioning, "XRAY_JSON", str(config_path))
    monkeypatch.setattr(xray, "core", Core())
    monkeypatch.setattr(xray, "hosts", Hosts())
    monkeypatch.setattr(xray, "nodes", {7: Node()})
    monkeypatch.setattr(provisioning.XRayConfig, "include_db_users", lambda self: self)
    monkeypatch.setattr(
        provisioning.xray.operations,
        "restart_node",
        lambda node_id, startup_config: node_restarts.append((node_id, startup_config)),
    )

    apply_provisioned_config(candidate_config)

    assert config_path.exists()
    assert restarts
    assert node_restarts[0][0] == 7
    assert host_updates == [True]
    assert xray.config.inbounds_by_tag["node-1-hy2-8443"]["protocol"] == "hysteria"


def test_apply_provisioned_config_restores_previous_file_when_restart_fails(
    monkeypatch, tmp_path
):
    from app import xray
    import app.xray.node_provisioning as provisioning

    config_path = tmp_path / "xray.json"
    previous_config = {
        "inbounds": [
            {
                "tag": "previous",
                "listen": "0.0.0.0",
                "port": 1080,
                "protocol": "shadowsocks",
                "settings": {"clients": [], "network": "tcp"},
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
    }
    config_path.write_text('{"inbounds": [], "outbounds": []}')
    xray.config = XRayConfig(previous_config)

    class Core:
        def restart(self, startup_config):
            raise RuntimeError("restart failed")

    class Hosts:
        def update(self):
            raise AssertionError("hosts must not refresh after failed restart")

    monkeypatch.setattr(provisioning, "XRAY_JSON", str(config_path))
    monkeypatch.setattr(xray, "core", Core())
    monkeypatch.setattr(xray, "hosts", Hosts())
    monkeypatch.setattr(xray, "nodes", {})
    monkeypatch.setattr(provisioning.XRayConfig, "include_db_users", lambda self: self)

    try:
        apply_provisioned_config(
            {
                "inbounds": [
                    {
                        "tag": "new",
                        "listen": "0.0.0.0",
                        "port": 8443,
                        "protocol": "hysteria",
                        "settings": {"version": 2, "users": []},
                        "streamSettings": {"network": "hysteria"},
                    }
                ],
                "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
            }
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected restart failure")

    assert config_path.read_text() == '{"inbounds": [], "outbounds": []}'
    assert xray.config.inbounds_by_tag["previous"]["protocol"] == "shadowsocks"


def test_redeem_node_install_payload_returns_one_time_config_without_private_key():
    db = _db_session()
    dbnode = _db_node(db)
    db.add(TLS(key="controller-private-key", certificate="controller-public-cert"))
    db.commit()
    token, _ = create_node_provision_token(
        db,
        node_id=dbnode.id,
        created_by="admin",
        active_inbounds=["node-1-hy2-8443"],
        core_kind="sing-box",
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    )

    payload = redeem_node_install_payload(
        db,
        token,
        binary_url="https://panel.example.com/download/marzban-node",
        sing_box_install_url="https://panel.example.com/download/install-sing-box.sh",
    )

    assert payload is not None
    assert payload.node_id == dbnode.id
    assert payload.core_kind == "sing-box"
    assert payload.active_inbounds == ["node-1-hy2-8443"]
    assert payload.ssl_client_cert == "controller-public-cert"
    assert payload.binary_url == "https://panel.example.com/download/marzban-node"
    assert payload.core_install_url == "https://panel.example.com/download/install-sing-box.sh"
    assert "INBOUNDS" not in payload.env
    assert "controller-private-key" not in payload.model_dump_json()
    assert redeem_node_install_payload(db, token) is None


def test_render_node_install_script_requires_token_and_does_not_write_inbounds():
    script = render_node_install_script("https://panel.example.com")

    assert "--token" in script
    assert "https://panel.example.com/api/node/provision/redeem" in script
    assert "binary_url" in script
    assert "core_install_url" in script
    assert "/usr/local/bin/marzban-node" in script
    assert "openssl req -x509" in script
    assert "/var/lib/marzban-node/ssl_cert.pem" in script
    assert "/var/lib/marzban-node/ssl_key.pem" in script
    assert "chmod 0600 /var/lib/marzban-node/ssl_key.pem" in script
    assert "INBOUNDS=" not in script
