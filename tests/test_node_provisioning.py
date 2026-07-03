import os
from datetime import datetime, timedelta

os.environ.setdefault("XRAY_EXECUTABLE_PATH", "/bin/echo")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import Node as DBNode
from app.db.crud import create_node_provision_token, redeem_node_provision_token
from app.models.node_provision import NodeProvisionProtocol
from app.xray.config import XRayConfig
from app.xray.node_provisioning import (
    build_generated_inbounds,
    choose_core_kind,
    hash_install_token,
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


def test_build_generated_inbounds_creates_hy2_vless_and_shadowsocks_templates():
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
    assert inbounds[2]["protocol"] == "shadowsocks"
    assert inbounds[2]["settings"]["clients"] == []


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
