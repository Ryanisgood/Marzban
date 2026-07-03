import hashlib
import hmac
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable, Sequence, Tuple

from sqlalchemy.orm import Session

from app import xray
from app.db import crud
from app.db.models import Node as DBNode, ProxyHost as DBProxyHost, ProxyInbound
from app.models.node import NodeInboundsMode
from app.models.node_provision import (
    NodeInstallPayload,
    NodeProvisionCreate,
    NodeProvisionProtocol,
)
from app.xray.config import XRayConfig
from config import (
    MARZBAN_NODE_BINARY_URL,
    SING_BOX_INSTALL_SCRIPT_URL,
    XRAY_INSTALL_SCRIPT_URL,
    XRAY_JSON,
)


ProtocolPort = Tuple[NodeProvisionProtocol, int]


@dataclass
class ProvisionNodeResult:
    node: DBNode
    active_inbounds: list[str]
    core_kind: str
    install_token: str
    install_command: str
    config: dict


def choose_core_kind(protocols: Iterable[NodeProvisionProtocol]) -> str:
    return "sing-box" if NodeProvisionProtocol.hy2 in set(protocols) else "xray"


def hash_install_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_install_token(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_install_token(token), token_hash)


def build_generated_inbounds(node_id: int, specs: Sequence[ProtocolPort]) -> list[dict]:
    return [
        _build_inbound(node_id=node_id, protocol=protocol, port=port)
        for protocol, port in specs
    ]


def _build_inbound(
    *, node_id: int, protocol: NodeProvisionProtocol, port: int
) -> dict:
    tag_protocol = "vless" if protocol == NodeProvisionProtocol.vless_reality else protocol.value
    tag = f"node-{node_id}-{tag_protocol}-{port}"

    if protocol == NodeProvisionProtocol.hy2:
        return {
            "tag": tag,
            "listen": "0.0.0.0",
            "port": port,
            "protocol": "hysteria",
            "settings": {"version": 2, "users": []},
            "streamSettings": {
                "network": "hysteria",
                "security": "tls",
                "tlsSettings": {"alpn": ["h3"], "certificates": []},
            },
        }

    if protocol == NodeProvisionProtocol.vless_reality:
        return {
            "tag": tag,
            "listen": "0.0.0.0",
            "port": port,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "publicKey": "generated-public-key",
                    "privateKey": "generated-private-key",
                    "shortIds": ["0123456789abcdef"],
                    "serverNames": ["example.com"],
                    "SpiderX": "/",
                },
            },
        }

    if protocol == NodeProvisionProtocol.shadowsocks:
        return {
            "tag": tag,
            "listen": "0.0.0.0",
            "port": port,
            "protocol": "shadowsocks",
            "settings": {"clients": [], "network": "tcp"},
        }

    raise ValueError(f"Unsupported provisioning protocol: {protocol}")


def provision_node(
    db: Session,
    payload: NodeProvisionCreate,
    *,
    admin_username: str,
    controller_url: str,
    current_config: dict,
    apply_config: Callable[[dict], None],
) -> ProvisionNodeResult:
    dbnode = DBNode(
        name=payload.name,
        address=payload.address,
        port=payload.port,
        api_port=payload.api_port,
        inbounds_mode=NodeInboundsMode.panel,
        usage_coefficient=payload.usage_coefficient,
    )
    db.add(dbnode)
    db.flush()

    specs = [(inbound.protocol, inbound.port) for inbound in payload.inbounds]
    generated_inbounds = build_generated_inbounds(dbnode.id, specs)
    active_tags = [inbound["tag"] for inbound in generated_inbounds]
    core_kind = choose_core_kind([inbound.protocol for inbound in payload.inbounds])

    candidate_config = deepcopy(current_config)
    candidate_config.setdefault("inbounds", [])
    candidate_config["inbounds"].extend(generated_inbounds)
    XRayConfig(candidate_config)

    inbound_rows = []
    for inbound, inbound_spec in zip(generated_inbounds, payload.inbounds):
        inbound_row = ProxyInbound(tag=inbound["tag"])
        db.add(inbound_row)
        db.add(
            DBProxyHost(
                remark=f"{payload.name} ({{USERNAME}}) [{{PROTOCOL}} - {{TRANSPORT}}]",
                address=payload.address,
                port=inbound_spec.port,
                inbound=inbound_row,
            )
        )
        inbound_rows.append(inbound_row)

    dbnode.active_inbound_objects = inbound_rows
    apply_config(candidate_config)

    install_token, _ = crud.create_node_provision_token(
        db,
        node_id=dbnode.id,
        created_by=admin_username,
        active_inbounds=active_tags,
        core_kind=core_kind,
        expires_at=datetime.utcnow() + timedelta(minutes=30),
    )
    install_command = (
        f"curl -fsSL {controller_url.rstrip('/')}/api/node/install.sh "
        f"| sudo bash -s -- --token {install_token}"
    )
    db.refresh(dbnode)

    return ProvisionNodeResult(
        node=dbnode,
        active_inbounds=active_tags,
        core_kind=core_kind,
        install_token=install_token,
        install_command=install_command,
        config=candidate_config,
    )


def apply_provisioned_config(payload: dict) -> None:
    config = XRayConfig(payload, api_port=xray.config.api_port)
    xray.config = config

    with open(XRAY_JSON, "w") as file:
        file.write(json.dumps(payload, indent=4))

    startup_config = xray.config.include_db_users()
    xray.core.restart(startup_config)
    for node_id, node in list(xray.nodes.items()):
        if node.connected:
            xray.operations.restart_node(node_id, startup_config)

    xray.hosts.update()


def redeem_node_install_payload(
    db: Session,
    token: str,
    *,
    binary_url: str = MARZBAN_NODE_BINARY_URL,
    xray_install_url: str = XRAY_INSTALL_SCRIPT_URL,
    sing_box_install_url: str = SING_BOX_INSTALL_SCRIPT_URL,
) -> NodeInstallPayload | None:
    record = crud.redeem_node_provision_token(db, token)
    if not record:
        return None

    tls = crud.get_tls_certificate(db)
    node = record.node
    env = {
        "SERVICE_HOST": "0.0.0.0",
        "SERVICE_PORT": str(node.port),
        "XRAY_API_HOST": "0.0.0.0",
        "XRAY_API_PORT": str(node.api_port),
        "XRAY_EXECUTABLE_PATH": "/usr/local/bin/xray",
        "XRAY_ASSETS_PATH": "/usr/local/share/xray",
        "SING_BOX_EXECUTABLE_PATH": "/usr/local/bin/sing-box",
        "SSL_CERT_FILE": "/var/lib/marzban-node/ssl_cert.pem",
        "SSL_KEY_FILE": "/var/lib/marzban-node/ssl_key.pem",
        "SSL_CLIENT_CERT_FILE": "/var/lib/marzban-node/ssl_client_cert.pem",
    }
    return NodeInstallPayload(
        node_id=node.id,
        node_name=node.name,
        service_port=node.port,
        api_port=node.api_port,
        active_inbounds=record.active_inbounds,
        core_kind=record.core_kind,
        ssl_client_cert=tls.certificate,
        binary_url=binary_url,
        core_install_url=sing_box_install_url
        if record.core_kind == "sing-box"
        else xray_install_url,
        env=env,
    )


def render_node_install_script(controller_url: str) -> str:
    redeem_url = f"{controller_url.rstrip('/')}/api/node/provision/redeem"
    return f"""#!/usr/bin/env bash
set -euo pipefail

TOKEN=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --token)
      TOKEN="${{2:-}}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ -z "$TOKEN" ]; then
  echo "--token is required" >&2
  exit 2
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 2
fi

PAYLOAD="$(curl -fsSL -X POST "{redeem_url}" \\
  -H "Content-Type: application/json" \\
  --data "{{\\"token\\":\\"$TOKEN\\"}}")"

BINARY_URL="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("binary_url", ""))' "$PAYLOAD")"
CORE_KIND="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("core_kind", ""))' "$PAYLOAD")"
CORE_INSTALL_URL="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("core_install_url", ""))' "$PAYLOAD")"

if [ -n "$BINARY_URL" ]; then
  curl -fsSL "$BINARY_URL" -o /usr/local/bin/marzban-node
  chmod 0755 /usr/local/bin/marzban-node
elif ! command -v marzban-node >/dev/null 2>&1; then
  echo "marzban-node is not installed and binary_url is empty" >&2
  exit 2
fi

if [ "$CORE_KIND" = "xray" ] && ! command -v xray >/dev/null 2>&1; then
  if [ -z "$CORE_INSTALL_URL" ]; then
    echo "xray is not installed and core_install_url is empty" >&2
    exit 2
  fi
  bash -c "$(curl -fsSL "$CORE_INSTALL_URL")" @ install
fi

if [ "$CORE_KIND" = "sing-box" ] && ! command -v sing-box >/dev/null 2>&1; then
  if [ -z "$CORE_INSTALL_URL" ]; then
    echo "sing-box is not installed and core_install_url is empty" >&2
    exit 2
  fi
  curl -fsSL "$CORE_INSTALL_URL" | sh
fi

install -d -m 0755 /var/lib/marzban-node
printf '%s' "$PAYLOAD" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ssl_client_cert"])' > /var/lib/marzban-node/ssl_client_cert.pem

python3 - "$PAYLOAD" >/etc/marzban-node.env <<'PY'
import json
import sys
payload = json.loads(sys.argv[1])
for key, value in payload["env"].items():
    print(f"{{key}}={{value}}")
PY

cat >/etc/systemd/system/marzban-node.service <<'SERVICE'
[Unit]
Description=Marzban Node Rust Service
After=network.target nss-lookup.target

[Service]
ExecStart=/usr/local/bin/marzban-node
Restart=on-failure
EnvironmentFile=-/etc/marzban-node.env

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable --now marzban-node
"""
