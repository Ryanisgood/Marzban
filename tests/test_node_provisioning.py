import os
from datetime import datetime, timedelta

os.environ.setdefault("XRAY_EXECUTABLE_PATH", "/bin/echo")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import Node as DBNode, NodeProvisionToken, ProxyInbound, TLS
from app.db.models import ProxyHost
from app.db.crud import create_node_provision_token, redeem_node_provision_token
from app.db import crud
from app.models.node import NodeInboundsMode
from app.models.node_provision import NodeProvisionCreate, NodeProvisionInbound
from app.models.node_provision import NodeProvisionProtocol, NodeProvisionRedeemRequest
from app.routers.node import redeem_node_provision
from app.xray.config import XRayConfig
from app.xray.node_provisioning import (
    apply_core_config_update,
    apply_provisioned_config,
    build_generated_inbounds,
    choose_core_kind,
    cleanup_orphaned_provisioned_inbounds,
    hash_install_token,
    provision_node,
    redeem_node_install_payload,
    remove_provisioned_node,
    render_node_install_script,
    validate_core_config_preserves_panel_inbounds,
    verify_install_token,
)


def test_node_provision_create_requires_at_least_one_inbound():
    try:
        NodeProvisionCreate(name="empty", address="node.example.com", inbounds=[])
    except ValueError as exc:
        assert "inbounds" in str(exc)
    else:
        raise AssertionError("expected provisioning request without inbounds to fail")


def test_node_provision_create_rejects_duplicate_protocol_port():
    try:
        NodeProvisionCreate(
            name="duplicate",
            address="node.example.com",
            inbounds=[
                NodeProvisionInbound(protocol=NodeProvisionProtocol.hy2, port=8443),
                NodeProvisionInbound(protocol=NodeProvisionProtocol.hy2, port=8443),
            ],
        )
    except ValueError as exc:
        assert "Duplicate protocol/port" in str(exc)
    else:
        raise AssertionError("expected duplicate protocol/port to fail")


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
            NodeProvisionProtocol.anytls,
            NodeProvisionProtocol.vless_reality,
            NodeProvisionProtocol.shadowsocks,
        ]
    ) == "sing-box"


def test_choose_core_kind_uses_sing_box_when_anytls_is_selected():
    assert choose_core_kind([NodeProvisionProtocol.anytls]) == "sing-box"


def test_choose_core_kind_uses_xray_for_xray_only_protocols():
    assert choose_core_kind([NodeProvisionProtocol.vless_reality]) == "xray"
    assert choose_core_kind([NodeProvisionProtocol.shadowsocks]) == "xray"


def test_install_token_hash_does_not_store_plaintext():
    token = "plain-token"
    token_hash = hash_install_token(token)

    assert token_hash != token
    assert verify_install_token(token, token_hash)
    assert not verify_install_token("other-token", token_hash)


def test_build_generated_inbounds_creates_hy2_anytls_vless_and_shadowsocks_templates(monkeypatch):
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
            (NodeProvisionProtocol.anytls, 9443),
            (NodeProvisionProtocol.vless_reality, 443),
            (NodeProvisionProtocol.shadowsocks, 8388),
        ],
    )

    assert [item["tag"] for item in inbounds] == [
        "node-42-hy2-8443",
        "node-42-anytls-9443",
        "node-42-vless-443",
        "node-42-shadowsocks-8388",
    ]
    assert inbounds[0]["protocol"] == "hysteria"
    assert inbounds[0]["settings"]["version"] == 2
    assert inbounds[0]["streamSettings"]["network"] == "hysteria"
    assert inbounds[1]["protocol"] == "anytls"
    assert inbounds[1]["settings"]["users"] == []
    assert inbounds[1]["streamSettings"]["network"] == "tcp"
    assert inbounds[1]["streamSettings"]["security"] == "tls"
    assert inbounds[2]["protocol"] == "vless"
    assert inbounds[2]["settings"]["clients"] == []
    reality_settings = inbounds[2]["streamSettings"]["realitySettings"]
    assert reality_settings["privateKey"] == "real-private-key"
    assert reality_settings["publicKey"] == "real-public-key"
    assert reality_settings["dest"] == "www.microsoft.com:443"
    assert reality_settings["serverNames"] == ["www.microsoft.com"]
    assert inbounds[3]["protocol"] == "shadowsocks"
    assert inbounds[3]["settings"]["clients"] == []


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


def test_build_generated_inbounds_uses_custom_vless_reality_server_name(monkeypatch):
    import app.xray.node_provisioning as provisioning

    monkeypatch.setattr(
        provisioning,
        "generate_reality_key_pair",
        lambda: ("real-private-key", "real-public-key"),
    )

    inbounds = build_generated_inbounds(
        node_id=42,
        specs=[
            NodeProvisionInbound(
                protocol=NodeProvisionProtocol.vless_reality,
                port=443,
                reality_server_name="cdn.example.com",
            )
        ],
    )

    reality_settings = inbounds[0]["streamSettings"]["realitySettings"]
    assert reality_settings["serverNames"] == ["cdn.example.com"]
    assert reality_settings["dest"] == "cdn.example.com:443"


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
        binary_url="https://panel.example.com/download/marzban-node",
        sing_box_install_url="https://panel.example.com/download/install-sing-box.sh",
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
    hy2_host = hosts[0]
    assert hy2_host.allowinsecure is True
    assert hy2_host.alpn.value == "h3"


def test_provision_node_sets_generated_inbound_owner_id(monkeypatch):
    import app.xray.node_provisioning as provisioning

    db = _db_session()
    monkeypatch.setattr(
        provisioning,
        "generate_reality_key_pair",
        lambda: ("real-private-key", "real-public-key"),
    )
    payload = NodeProvisionCreate(
        name="owned-node",
        address="203.0.113.10",
        inbounds=[
            NodeProvisionInbound(protocol=NodeProvisionProtocol.vless_reality, port=443),
        ],
    )

    result = provision_node(
        db,
        payload,
        admin_username="admin",
        controller_url="https://panel.example.com",
        current_config={
            "log": {"loglevel": "warning"},
            "inbounds": [],
            "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
        },
        apply_config=lambda config: None,
        binary_url="https://panel.example.com/download/marzban-node",
        xray_install_url="https://panel.example.com/download/install-xray.sh",
        sing_box_install_url="https://panel.example.com/download/install-sing-box.sh",
    )

    inbound = db.query(ProxyInbound).filter_by(tag=result.active_inbounds[0]).one()
    assert inbound.owner_node_id == result.node.id


def test_provision_node_does_not_apply_config_when_token_creation_fails(monkeypatch):
    import app.xray.node_provisioning as provisioning

    db = _db_session()
    applied_configs = []
    payload = NodeProvisionCreate(
        name="rn1c1g",
        address="node.example.com",
        inbounds=[
            NodeProvisionInbound(protocol=NodeProvisionProtocol.hy2, port=8443),
        ],
    )
    current_config = {
        "inbounds": [],
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
    }

    def fail_token_creation(*args, **kwargs):
        raise RuntimeError("token insert failed")

    monkeypatch.setattr(
        provisioning.crud,
        "create_node_provision_token",
        fail_token_creation,
    )

    try:
        provision_node(
            db,
            payload,
            admin_username="admin",
            controller_url="https://panel.example.com",
            current_config=current_config,
            apply_config=applied_configs.append,
            binary_url="https://panel.example.com/download/marzban-node",
            sing_box_install_url="https://panel.example.com/download/install-sing-box.sh",
        )
    except RuntimeError as exc:
        assert "token insert failed" in str(exc)
    else:
        raise AssertionError("expected token creation failure")

    assert applied_configs == []


def test_provision_node_rolls_back_db_when_config_apply_fails(monkeypatch):
    db = _db_session()
    payload = NodeProvisionCreate(
        name="rn1c1g",
        address="node.example.com",
        inbounds=[
            NodeProvisionInbound(protocol=NodeProvisionProtocol.hy2, port=8443),
        ],
    )
    current_config = {
        "inbounds": [],
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
    }

    def fail_apply_config(config):
        raise RuntimeError("core restart failed")

    try:
        provision_node(
            db,
            payload,
            admin_username="admin",
            controller_url="https://panel.example.com",
            current_config=current_config,
            apply_config=fail_apply_config,
            binary_url="https://panel.example.com/download/marzban-node",
            sing_box_install_url="https://panel.example.com/download/install-sing-box.sh",
        )
    except RuntimeError as exc:
        assert "core restart failed" in str(exc)
    else:
        raise AssertionError("expected config apply failure")

    assert db.query(DBNode).count() == 0
    assert db.query(NodeProvisionToken).count() == 0


def test_provision_node_rejects_conflicting_tcp_ports():
    db = _db_session()
    payload = NodeProvisionCreate(
        name="rn1c1g",
        address="node.example.com",
        inbounds=[
            NodeProvisionInbound(protocol=NodeProvisionProtocol.vless_reality, port=443),
            NodeProvisionInbound(protocol=NodeProvisionProtocol.shadowsocks, port=443),
        ],
    )

    try:
        provision_node(
            db,
            payload,
            admin_username="admin",
            controller_url="https://panel.example.com",
            current_config={"inbounds": [], "outbounds": []},
            apply_config=lambda config: None,
            binary_url="https://panel.example.com/download/marzban-node",
        )
    except ValueError as exc:
        assert "port conflict" in str(exc)
    else:
        raise AssertionError("expected TCP port conflict")


def test_provision_node_rejects_conflict_with_existing_inbound_port(monkeypatch):
    import app.xray.node_provisioning as provisioning

    db = _db_session()
    monkeypatch.setattr(
        provisioning,
        "generate_reality_key_pair",
        lambda: ("real-private-key", "real-public-key"),
    )
    payload = NodeProvisionCreate(
        name="rn1c1g",
        address="node.example.com",
        inbounds=[
            NodeProvisionInbound(protocol=NodeProvisionProtocol.vless_reality, port=443),
        ],
    )
    current_config = {
        "inbounds": [
            {
                "tag": "existing-ss",
                "listen": "0.0.0.0",
                "port": 443,
                "protocol": "shadowsocks",
                "settings": {"clients": [], "network": "tcp"},
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
    }

    try:
        provision_node(
            db,
            payload,
            admin_username="admin",
            controller_url="https://panel.example.com",
            current_config=current_config,
            apply_config=lambda config: None,
            binary_url="https://panel.example.com/download/marzban-node",
        )
    except ValueError as exc:
        assert "port conflict" in str(exc)
    else:
        raise AssertionError("expected existing inbound port conflict")


def test_provision_node_requires_binary_url_for_one_command_install():
    db = _db_session()
    payload = NodeProvisionCreate(
        name="rn1c1g",
        address="node.example.com",
        inbounds=[
            NodeProvisionInbound(protocol=NodeProvisionProtocol.shadowsocks, port=8388),
        ],
    )

    try:
        provision_node(
            db,
            payload,
            admin_username="admin",
            controller_url="https://panel.example.com",
            current_config={"inbounds": [], "outbounds": []},
            apply_config=lambda config: None,
            binary_url="",
        )
    except ValueError as exc:
        assert "MARZBAN_NODE_BINARY_URL" in str(exc)
    else:
        raise AssertionError("expected missing binary URL to fail")


def test_provision_node_requires_sing_box_install_source_for_hy2():
    db = _db_session()
    payload = NodeProvisionCreate(
        name="rn1c1g",
        address="node.example.com",
        inbounds=[
            NodeProvisionInbound(protocol=NodeProvisionProtocol.hy2, port=8443),
        ],
    )

    try:
        provision_node(
            db,
            payload,
            admin_username="admin",
            controller_url="https://panel.example.com",
            current_config={"inbounds": [], "outbounds": []},
            apply_config=lambda config: None,
            binary_url="https://panel.example.com/download/marzban-node",
            sing_box_install_url="",
            sing_box_download_url_template="",
        )
    except ValueError as exc:
        assert "SING_BOX_INSTALL_SCRIPT_URL or SING_BOX_DOWNLOAD_URL_TEMPLATE" in str(exc)
    else:
        raise AssertionError("expected missing sing-box install source to fail")


def test_provision_node_rejects_service_port_conflict():
    db = _db_session()
    payload = NodeProvisionCreate(
        name="rn1c1g",
        address="node.example.com",
        port=8443,
        inbounds=[
            NodeProvisionInbound(protocol=NodeProvisionProtocol.hy2, port=8443),
        ],
    )

    try:
        provision_node(
            db,
            payload,
            admin_username="admin",
            controller_url="https://panel.example.com",
            current_config={"inbounds": [], "outbounds": []},
            apply_config=lambda config: None,
            binary_url="https://panel.example.com/download/marzban-node",
            sing_box_install_url="https://panel.example.com/download/install-sing-box.sh",
        )
    except ValueError as exc:
        assert "service port" in str(exc)
    else:
        raise AssertionError("expected service port conflict")


def test_provision_node_rejects_api_port_conflict_for_xray_core():
    db = _db_session()
    payload = NodeProvisionCreate(
        name="rn1c1g",
        address="node.example.com",
        api_port=443,
        inbounds=[
            NodeProvisionInbound(protocol=NodeProvisionProtocol.shadowsocks, port=443),
        ],
    )

    try:
        provision_node(
            db,
            payload,
            admin_username="admin",
            controller_url="https://panel.example.com",
            current_config={"inbounds": [], "outbounds": []},
            apply_config=lambda config: None,
            binary_url="https://panel.example.com/download/marzban-node",
        )
    except ValueError as exc:
        assert "api port" in str(exc)
    else:
        raise AssertionError("expected api port conflict")


def test_provision_node_rejects_wildcard_bind_conflict(monkeypatch):
    import app.xray.node_provisioning as provisioning

    db = _db_session()
    monkeypatch.setattr(
        provisioning,
        "generate_reality_key_pair",
        lambda: ("real-private-key", "real-public-key"),
    )
    payload = NodeProvisionCreate(
        name="rn1c1g",
        address="node.example.com",
        inbounds=[
            NodeProvisionInbound(protocol=NodeProvisionProtocol.vless_reality, port=443),
        ],
    )
    current_config = {
        "inbounds": [
            {
                "tag": "existing-ss",
                "listen": "::",
                "port": 443,
                "protocol": "shadowsocks",
                "settings": {"clients": [], "network": "tcp"},
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
    }

    try:
        provision_node(
            db,
            payload,
            admin_username="admin",
            controller_url="https://panel.example.com",
            current_config=current_config,
            apply_config=lambda config: None,
            binary_url="https://panel.example.com/download/marzban-node",
        )
    except ValueError as exc:
        assert "port conflict" in str(exc)
    else:
        raise AssertionError("expected wildcard bind conflict")


def test_provision_node_rejects_tcp_udp_existing_inbound_udp_conflict():
    db = _db_session()
    payload = NodeProvisionCreate(
        name="rn1c1g",
        address="node.example.com",
        inbounds=[
            NodeProvisionInbound(protocol=NodeProvisionProtocol.hy2, port=8443),
        ],
    )
    current_config = {
        "inbounds": [
            {
                "tag": "existing-ss",
                "listen": "0.0.0.0",
                "port": 8443,
                "protocol": "shadowsocks",
                "settings": {"clients": [], "network": "tcp,udp"},
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
    }

    try:
        provision_node(
            db,
            payload,
            admin_username="admin",
            controller_url="https://panel.example.com",
            current_config=current_config,
            apply_config=lambda config: None,
            binary_url="https://panel.example.com/download/marzban-node",
            sing_box_install_url="https://panel.example.com/download/install-sing-box.sh",
        )
    except ValueError as exc:
        assert "port conflict" in str(exc)
    else:
        raise AssertionError("expected tcp,udp conflict")


def test_provision_node_uses_latest_config_inside_apply_lock(monkeypatch):
    import app.xray.node_provisioning as provisioning

    db = _db_session()
    configs = [
        {
            "inbounds": [
                {
                    "tag": "node-99-hy2-8443",
                    "listen": "0.0.0.0",
                    "port": 8443,
                    "protocol": "hysteria",
                    "settings": {"version": 2, "users": []},
                    "streamSettings": {"network": "hysteria"},
                }
            ],
            "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
        }
    ]
    payload = NodeProvisionCreate(
        name="rn1c1g",
        address="node.example.com",
        inbounds=[
            NodeProvisionInbound(protocol=NodeProvisionProtocol.hy2, port=9443),
        ],
    )

    def current_config_provider():
        return configs[-1]

    def apply_config(config):
        configs.append(config)

    provision_node(
        db,
        payload,
        admin_username="admin",
        controller_url="https://panel.example.com",
        current_config={"inbounds": [], "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}]},
        current_config_provider=current_config_provider,
        apply_config=apply_config,
        binary_url="https://panel.example.com/download/marzban-node",
        sing_box_install_url="https://panel.example.com/download/install-sing-box.sh",
    )

    applied_tags = [item["tag"] for item in configs[-1]["inbounds"]]
    assert "node-99-hy2-8443" in applied_tags
    assert "node-1-hy2-9443" in applied_tags


def test_node_provision_token_node_fk_cascades_on_delete():
    foreign_key = next(iter(NodeProvisionToken.__table__.c.node_id.foreign_keys))

    assert foreign_key.ondelete == "CASCADE"


def test_core_config_preserve_check_rejects_missing_panel_inbound():
    db = _db_session()
    dbnode = _db_node(db)
    dbnode.inbounds_mode = NodeInboundsMode.panel
    inbound = ProxyInbound(tag="node-1-hy2-8443", owner_node_id=dbnode.id)
    db.add(inbound)
    db.flush()
    dbnode.active_inbound_objects = [inbound]
    db.commit()

    try:
        validate_core_config_preserves_panel_inbounds(
            db, {"inbounds": [], "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}]}
        )
    except ValueError as exc:
        assert "panel-managed inbound" in str(exc)
    else:
        raise AssertionError("expected missing panel inbound to be rejected")


def test_core_config_preserve_check_rejects_missing_owned_inbound_when_deselected():
    db = _db_session()
    dbnode = _db_node(db)
    dbnode.inbounds_mode = NodeInboundsMode.panel
    db.add(ProxyInbound(tag="node-1-vless-443", owner_node_id=dbnode.id))
    db.commit()

    try:
        validate_core_config_preserves_panel_inbounds(
            db, {"inbounds": [], "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}]}
        )
    except ValueError as exc:
        assert "node-1-vless-443" in str(exc)
    else:
        raise AssertionError("expected missing owned inbound to be rejected")


def test_cleanup_orphaned_provisioned_inbounds_removes_generated_tags_without_db_owner():
    db = _db_session()
    config = {
        "inbounds": [
            {
                "tag": "node-99-hy2-8443",
                "listen": "0.0.0.0",
                "port": 8443,
                "protocol": "hysteria",
                "settings": {"version": 2, "users": []},
                "streamSettings": {"network": "hysteria"},
            },
            {
                "tag": "manual",
                "listen": "0.0.0.0",
                "port": 1080,
                "protocol": "shadowsocks",
                "settings": {"clients": [], "network": "tcp"},
            },
        ],
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
    }

    cleaned, removed = cleanup_orphaned_provisioned_inbounds(db, config)

    assert removed == ["node-99-hy2-8443"]
    assert [inbound["tag"] for inbound in cleaned["inbounds"]] == ["manual"]


def test_cleanup_orphaned_provisioned_inbounds_removes_vless_generated_tags():
    db = _db_session()
    config = {
        "inbounds": [
            {
                "tag": "node-99-vless-443",
                "listen": "0.0.0.0",
                "port": 443,
                "protocol": "vless",
                "settings": {"clients": []},
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
    }

    cleaned, removed = cleanup_orphaned_provisioned_inbounds(db, config)

    assert removed == ["node-99-vless-443"]
    assert cleaned["inbounds"] == []


def test_apply_core_config_update_validates_and_applies_inside_lock(monkeypatch):
    import app.xray.node_provisioning as provisioning

    db = _db_session()
    events = []

    class TrackingLock:
        def __enter__(self):
            events.append("enter")

        def __exit__(self, exc_type, exc, traceback):
            events.append("exit")

    monkeypatch.setattr(provisioning, "_config_apply_lock", TrackingLock())
    monkeypatch.setattr(
        provisioning,
        "validate_core_config_preserves_panel_inbounds",
        lambda db, payload: events.append("validate"),
    )
    monkeypatch.setattr(
        provisioning,
        "cleanup_orphaned_provisioned_inbounds",
        lambda db, payload: (payload, []),
    )
    monkeypatch.setattr(
        provisioning,
        "_apply_provisioned_config",
        lambda payload: events.append("apply"),
    )

    apply_core_config_update(
        db,
        {
            "inbounds": [
                {
                    "tag": "manual",
                    "listen": "0.0.0.0",
                    "port": 1080,
                    "protocol": "shadowsocks",
                    "settings": {"clients": [], "network": "tcp"},
                }
            ],
            "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
        },
    )

    assert events == ["enter", "validate", "apply", "exit"]


def test_redeem_node_provision_endpoint_rejects_client_consumption():
    db = _db_session()

    try:
        redeem_node_provision(
            NodeProvisionRedeemRequest(token="token", consume=True),
            db,
        )
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
        assert "finalized by controller" in exc.detail
    else:
        raise AssertionError("expected client consume request to fail")


def test_remove_provisioned_node_removes_generated_inbound_rows_and_config(monkeypatch):
    from app import xray
    import app.xray.node_provisioning as provisioning

    db = _db_session()
    dbnode = _db_node(db)
    dbnode.inbounds_mode = NodeInboundsMode.panel
    inbound = ProxyInbound(tag="node-1-hy2-8443", owner_node_id=dbnode.id)
    db.add(inbound)
    db.flush()
    dbnode.active_inbound_objects = [inbound]
    db.commit()
    xray.config = XRayConfig(
        {
            "inbounds": [
                {
                    "tag": "node-1-hy2-8443",
                    "listen": "0.0.0.0",
                    "port": 8443,
                    "protocol": "hysteria",
                    "settings": {"version": 2, "users": []},
                    "streamSettings": {"network": "hysteria"},
                },
                {
                    "tag": "manual",
                    "listen": "0.0.0.0",
                    "port": 1080,
                    "protocol": "shadowsocks",
                    "settings": {"clients": [], "network": "tcp"},
                },
            ],
            "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
        }
    )

    applied_configs = []
    monkeypatch.setattr(
        provisioning,
        "_apply_provisioned_config",
        lambda payload: applied_configs.append(payload),
    )

    removed_tags = remove_provisioned_node(db, dbnode)

    assert removed_tags == ["node-1-hy2-8443"]
    assert db.query(ProxyInbound).filter(ProxyInbound.tag == "node-1-hy2-8443").first() is None
    applied_tags = [inbound["tag"] for inbound in applied_configs[-1]["inbounds"]]
    assert "node-1-hy2-8443" not in applied_tags
    assert "manual" in applied_tags


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


def test_apply_provisioned_config_serializes_with_provisioning_lock(monkeypatch):
    import app.xray.node_provisioning as provisioning

    lock_events = []

    class TrackingLock:
        def __enter__(self):
            lock_events.append("enter")

        def __exit__(self, exc_type, exc, traceback):
            lock_events.append("exit")

    monkeypatch.setattr(provisioning, "_config_apply_lock", TrackingLock())
    monkeypatch.setattr(
        provisioning,
        "_apply_provisioned_config",
        lambda payload: lock_events.append("apply"),
    )

    apply_provisioned_config({"inbounds": [], "outbounds": []})

    assert lock_events == ["enter", "apply", "exit"]


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
    assert payload.core_version == "1.13.14"
    assert "sing-box-{version}-linux-{arch}.tar.gz" in payload.core_download_url_template
    assert "INBOUNDS" not in payload.env
    assert "controller-private-key" not in payload.model_dump_json()
    assert redeem_node_install_payload(db, token) is None


def test_redeem_node_install_payload_uses_configured_sing_box_version():
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
        sing_box_install_url="https://sing-box.app/install.sh",
        sing_box_version="1.13.14",
        sing_box_download_url_template=(
            "https://mirror.example.com/sing-box-{version}-linux-{arch}.tar.gz"
        ),
    )

    assert payload is not None
    assert payload.core_install_url == "https://sing-box.app/install.sh"
    assert payload.core_version == "1.13.14"
    assert payload.core_download_url_template == (
        "https://mirror.example.com/sing-box-{version}-linux-{arch}.tar.gz"
    )


def test_redeem_node_install_payload_omits_core_version_for_xray():
    db = _db_session()
    dbnode = _db_node(db)
    db.add(TLS(key="controller-private-key", certificate="controller-public-cert"))
    db.commit()
    token, _ = create_node_provision_token(
        db,
        node_id=dbnode.id,
        created_by="admin",
        active_inbounds=["node-1-vless-443"],
        core_kind="xray",
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    )

    payload = redeem_node_install_payload(
        db,
        token,
        binary_url="https://panel.example.com/download/marzban-node",
        xray_install_url="https://github.com/XTLS/Xray-install/raw/main/install-release.sh",
        sing_box_version="1.13.14",
    )

    assert payload is not None
    assert payload.core_version == ""
    assert payload.core_download_url_template == ""


def test_redeem_node_install_payload_can_peek_before_consuming_token():
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

    peeked = redeem_node_install_payload(
        db,
        token,
        consume=False,
        binary_url="https://panel.example.com/download/marzban-node",
        sing_box_install_url="https://panel.example.com/download/install-sing-box.sh",
    )
    consumed = redeem_node_install_payload(
        db,
        token,
        consume=True,
        binary_url="https://panel.example.com/download/marzban-node",
        sing_box_install_url="https://panel.example.com/download/install-sing-box.sh",
    )

    assert peeked is not None
    assert consumed is not None
    assert redeem_node_install_payload(db, token, consume=True) is None


def test_remove_provisioned_node_removes_owned_inbounds_even_when_deselected(monkeypatch):
    from app import xray
    import app.xray.node_provisioning as provisioning

    db = _db_session()
    dbnode = _db_node(db)
    dbnode.inbounds_mode = NodeInboundsMode.panel
    other_node = DBNode(
        name="node-2",
        address="203.0.113.20",
        port=62050,
        api_port=62051,
        inbounds_mode=NodeInboundsMode.panel,
    )
    db.add(other_node)
    db.flush()
    db.add_all([
        ProxyInbound(tag="node-1-vless-443", owner_node_id=dbnode.id),
        ProxyInbound(tag="node-2-vless-443", owner_node_id=other_node.id),
    ])
    db.commit()
    xray.config = XRayConfig(
        {
            "inbounds": [
                {"tag": "node-1-vless-443", "protocol": "vless", "port": 443},
                {"tag": "node-2-vless-443", "protocol": "vless", "port": 8443},
            ],
            "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
        }
    )
    applied = []
    monkeypatch.setattr(provisioning, "_apply_provisioned_config", applied.append)

    removed = remove_provisioned_node(db, dbnode)

    assert removed == ["node-1-vless-443"]
    assert db.query(ProxyInbound).filter_by(tag="node-1-vless-443").first() is None
    assert db.query(ProxyInbound).filter_by(tag="node-2-vless-443").first() is not None
    applied_tags = [inbound["tag"] for inbound in applied[-1]["inbounds"]]
    assert "node-1-vless-443" not in applied_tags
    assert "node-2-vless-443" in applied_tags


def test_remove_node_revokes_pending_provision_tokens():
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

    crud.remove_node(db, dbnode)

    assert (
        redeem_node_install_payload(
            db,
            token,
            consume=False,
            binary_url="https://panel.example.com/download/marzban-node",
            sing_box_install_url="https://panel.example.com/download/install-sing-box.sh",
        )
        is None
    )


def test_redeem_node_provision_tokens_for_node_consumes_pending_install_token():
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

    assert (
        redeem_node_install_payload(
            db,
            token,
            consume=False,
            binary_url="https://panel.example.com/download/marzban-node",
            sing_box_install_url="https://panel.example.com/download/install-sing-box.sh",
        )
        is not None
    )

    assert crud.redeem_node_provision_tokens_for_node(db, dbnode.id) == 1
    assert (
        redeem_node_install_payload(
            db,
            token,
            consume=False,
            binary_url="https://panel.example.com/download/marzban-node",
            sing_box_install_url="https://panel.example.com/download/install-sing-box.sh",
        )
        is None
    )


def test_render_node_install_script_requires_token_and_does_not_write_inbounds():
    script = render_node_install_script("https://panel.example.com")

    assert "--token" in script
    assert "https://panel.example.com/api/node/provision/redeem" in script
    assert "binary_url" in script
    assert "core_install_url" in script
    assert "core_version" in script
    assert "core_download_url_template" in script
    assert "installed_sing_box_version" in script
    assert "install_sing_box_release" in script
    assert "sing-box-{version}-linux-{arch}.tar.gz" not in script
    assert 'sh -s -- --version "$CORE_VERSION"' in script
    assert "does not match required" in script
    assert '\\"consume\\":false' in script
    assert '\\"consume\\":true' not in script
    assert "systemctl is-active --quiet marzban-node" in script
    assert "/usr/local/bin/marzban-node" in script
    assert "openssl req -x509" in script
    assert "/var/lib/marzban-node/ssl_cert.pem" in script
    assert "/var/lib/marzban-node/ssl_key.pem" in script
    assert "chmod 0600 /var/lib/marzban-node/ssl_key.pem" in script
    assert "INBOUNDS=" not in script
