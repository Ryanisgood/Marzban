import os

os.environ.setdefault("XRAY_EXECUTABLE_PATH", "/bin/echo")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import xray
from app.db.base import Base
from app.db.models import Node, ProxyInbound
from app.models.node import NodeInboundsMode
from app.routers.system import build_inbounds_response
from app.xray.config import XRayConfig


def test_build_inbounds_response_includes_owner_node_id(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    config = XRayConfig(
        {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {"tag": "node-1-vless-443", "protocol": "vless", "port": 443},
                {"tag": "legacy-vless", "protocol": "vless", "port": 8443},
            ],
            "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
        }
    )
    monkeypatch.setattr(xray, "config", config)

    with TestingSession() as db:
        node = Node(
            name="n1",
            address="203.0.113.1",
            port=62050,
            api_port=62051,
            inbounds_mode=NodeInboundsMode.panel,
        )
        db.add(node)
        db.flush()
        db.add(ProxyInbound(tag="node-1-vless-443", owner_node_id=node.id))
        db.commit()

        response = build_inbounds_response(db)

        assert db.query(ProxyInbound).count() == 1

    owned, legacy = response["vless"]
    assert owned["owner_node_id"] == 1
    assert legacy["owner_node_id"] is None
