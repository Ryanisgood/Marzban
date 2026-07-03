import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
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
_config_apply_lock = threading.RLock()


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


def generate_reality_key_pair() -> tuple[str, str] | None:
    key_pair = xray.core.get_x25519()
    if not key_pair:
        return None
    return key_pair["private_key"], key_pair["public_key"]


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
        key_pair = generate_reality_key_pair()
        if not key_pair:
            raise ValueError(
                "Unable to generate x25519 key pair for VLESS REALITY inbound"
            )
        private_key, public_key = key_pair
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
                    "publicKey": public_key,
                    "privateKey": private_key,
                    "shortIds": [secrets.token_hex(8)],
                    "serverNames": ["www.microsoft.com"],
                    "dest": "www.microsoft.com:443",
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
    binary_url: str = MARZBAN_NODE_BINARY_URL,
    xray_install_url: str = XRAY_INSTALL_SCRIPT_URL,
    sing_box_install_url: str = SING_BOX_INSTALL_SCRIPT_URL,
) -> ProvisionNodeResult:
    specs = [(inbound.protocol, inbound.port) for inbound in payload.inbounds]
    core_kind = choose_core_kind([inbound.protocol for inbound in payload.inbounds])
    validate_install_sources(
        core_kind,
        binary_url=binary_url,
        xray_install_url=xray_install_url,
        sing_box_install_url=sing_box_install_url,
    )
    validate_requested_port_conflicts(current_config, specs)

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

    generated_inbounds = build_generated_inbounds(dbnode.id, specs)
    validate_generated_inbound_conflicts(current_config, generated_inbounds)
    active_tags = [inbound["tag"] for inbound in generated_inbounds]

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
    install_token, _ = crud.create_node_provision_token(
        db,
        node_id=dbnode.id,
        created_by=admin_username,
        active_inbounds=active_tags,
        core_kind=core_kind,
        expires_at=datetime.utcnow() + timedelta(minutes=30),
        commit=False,
    )
    applied_config = False
    try:
        apply_config(candidate_config)
        applied_config = True
        db.commit()
    except Exception:
        db.rollback()
        if applied_config:
            apply_config(current_config)
        raise

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


def validate_install_sources(
    core_kind: str,
    *,
    binary_url: str,
    xray_install_url: str,
    sing_box_install_url: str,
) -> None:
    if not binary_url:
        raise ValueError(
            "MARZBAN_NODE_BINARY_URL must be configured for one-command node install"
        )
    if core_kind == "xray" and not xray_install_url:
        raise ValueError(
            "XRAY_INSTALL_SCRIPT_URL must be configured for Xray node install"
        )
    if core_kind == "sing-box" and not sing_box_install_url:
        raise ValueError(
            "SING_BOX_INSTALL_SCRIPT_URL must be configured for HY2/sing-box node install"
        )


def validate_generated_inbound_conflicts(
    current_config: dict, generated_inbounds: Sequence[dict]
) -> None:
    occupied = {}
    for inbound in current_config.get("inbounds", []):
        endpoint = _inbound_endpoint(inbound)
        if endpoint:
            occupied.setdefault(endpoint, inbound.get("tag", "<existing>"))

    generated_seen = {}
    for inbound in generated_inbounds:
        endpoint = _inbound_endpoint(inbound)
        if not endpoint:
            continue
        tag = inbound["tag"]
        if endpoint in generated_seen:
            raise ValueError(
                f"Generated inbound port conflict: {tag} conflicts with {generated_seen[endpoint]}"
            )
        if endpoint in occupied:
            raise ValueError(
                f"Generated inbound port conflict: {tag} conflicts with {occupied[endpoint]}"
            )
        generated_seen[endpoint] = tag


def validate_requested_port_conflicts(
    current_config: dict, specs: Sequence[ProtocolPort]
) -> None:
    occupied = {}
    for inbound in current_config.get("inbounds", []):
        endpoint = _inbound_endpoint(inbound)
        if endpoint:
            occupied.setdefault(endpoint, inbound.get("tag", "<existing>"))

    generated_seen = {}
    for protocol, port in specs:
        endpoint = ("0.0.0.0", port, _protocol_transport(protocol))
        label = f"{protocol.value}:{port}"
        if endpoint in generated_seen:
            raise ValueError(
                f"Generated inbound port conflict: {label} conflicts with {generated_seen[endpoint]}"
            )
        if endpoint in occupied:
            raise ValueError(
                f"Generated inbound port conflict: {label} conflicts with {occupied[endpoint]}"
            )
        generated_seen[endpoint] = label


def _inbound_endpoint(inbound: dict) -> tuple[str, int, str] | None:
    port = inbound.get("port")
    if port is None:
        return None
    try:
        normalized_port = int(port)
    except (TypeError, ValueError):
        return None

    listen = inbound.get("listen") or "0.0.0.0"
    return listen, normalized_port, _inbound_transport(inbound)


def _protocol_transport(protocol: NodeProvisionProtocol) -> str:
    return "udp" if protocol == NodeProvisionProtocol.hy2 else "tcp"


def _inbound_transport(inbound: dict) -> str:
    stream_settings = inbound.get("streamSettings") or {}
    network = stream_settings.get("network")
    if network in {"hysteria", "hysteria2", "quic"}:
        return "udp"
    settings = inbound.get("settings") or {}
    if settings.get("network") == "udp":
        return "udp"
    return "tcp"


def apply_provisioned_config(payload: dict) -> None:
    with _config_apply_lock:
        _apply_provisioned_config(payload)


def _apply_provisioned_config(payload: dict) -> None:
    previous_config = xray.config
    previous_file = _read_optional_file(XRAY_JSON)
    try:
        config = XRayConfig(payload, api_port=xray.config.api_port)
        xray.config = config
        _atomic_write_text(XRAY_JSON, json.dumps(payload, indent=4))
        startup_config = xray.config.include_db_users()
        xray.core.restart(startup_config)
        for node_id, node in list(xray.nodes.items()):
            if node.connected:
                xray.operations.restart_node(node_id, startup_config)
    except Exception:
        xray.config = previous_config
        _restore_optional_file(XRAY_JSON, previous_file)
        raise

    xray.hosts.update()


def _read_optional_file(path: str) -> bytes | None:
    try:
        with open(path, "rb") as file:
            return file.read()
    except FileNotFoundError:
        return None


def _restore_optional_file(path: str, content: bytes | None) -> None:
    if content is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    _atomic_write_bytes(path, content)


def _atomic_write_text(path: str, content: str) -> None:
    _atomic_write_bytes(path, content.encode())


def _atomic_write_bytes(path: str, content: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


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
if [ ! -s /var/lib/marzban-node/ssl_cert.pem ] || [ ! -s /var/lib/marzban-node/ssl_key.pem ]; then
  if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required to generate node TLS certificate" >&2
    exit 2
  fi
  openssl req -x509 -newkey rsa:2048 -nodes \\
    -keyout /var/lib/marzban-node/ssl_key.pem \\
    -out /var/lib/marzban-node/ssl_cert.pem \\
    -sha256 -days 3650 -subj "/CN=marzban-node"
  chmod 0600 /var/lib/marzban-node/ssl_key.pem
  chmod 0644 /var/lib/marzban-node/ssl_cert.pem
fi
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
