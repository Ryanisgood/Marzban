import hashlib
import hmac
import json
import os
import re
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
    NodeProvisionInbound,
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
ProvisionInboundSpec = ProtocolPort | NodeProvisionInbound
_config_apply_lock = threading.RLock()
_GENERATED_INBOUND_TAG = re.compile(
    r"^node-\d+-(hy2|anytls|vless|vless-reality|shadowsocks)-\d+$"
)
SING_BOX_ONLY_PROVISION_PROTOCOLS = {
    NodeProvisionProtocol.hy2,
    NodeProvisionProtocol.anytls,
}


@dataclass
class ProvisionNodeResult:
    node: DBNode
    active_inbounds: list[str]
    core_kind: str
    install_token: str
    install_command: str
    config: dict


def choose_core_kind(protocols: Iterable[NodeProvisionProtocol]) -> str:
    return (
        "sing-box"
        if set(protocols) & SING_BOX_ONLY_PROVISION_PROTOCOLS
        else "xray"
    )


def hash_install_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_install_token(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_install_token(token), token_hash)


def build_generated_inbounds(
    node_id: int, specs: Sequence[ProvisionInboundSpec]
) -> list[dict]:
    normalized_specs = [_normalize_inbound_spec(spec) for spec in specs]
    return [
        _build_inbound(
            node_id=node_id,
            protocol=protocol,
            port=port,
            reality_server_name=reality_server_name,
        )
        for protocol, port, reality_server_name in normalized_specs
    ]


def _normalize_inbound_spec(
    spec: ProvisionInboundSpec,
) -> tuple[NodeProvisionProtocol, int, str | None]:
    if isinstance(spec, NodeProvisionInbound):
        return spec.protocol, spec.port, spec.reality_server_name
    protocol, port = spec
    return protocol, port, None


def generate_reality_key_pair() -> tuple[str, str] | None:
    key_pair = xray.core.get_x25519()
    if not key_pair:
        return None
    return key_pair["private_key"], key_pair["public_key"]


def _build_inbound(
    *,
    node_id: int,
    protocol: NodeProvisionProtocol,
    port: int,
    reality_server_name: str | None = None,
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

    if protocol == NodeProvisionProtocol.anytls:
        return {
            "tag": tag,
            "listen": "0.0.0.0",
            "port": port,
            "protocol": "anytls",
            "settings": {"users": []},
            "streamSettings": {
                "network": "tcp",
                "security": "tls",
                "tlsSettings": {
                    "alpn": ["h2", "http/1.1"],
                    "certificates": [],
                },
            },
        }

    if protocol == NodeProvisionProtocol.vless_reality:
        server_name = reality_server_name or "www.microsoft.com"
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
                    "serverNames": [server_name],
                    "dest": f"{server_name}:443",
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
    current_config_provider: Callable[[], dict] | None = None,
    binary_url: str = MARZBAN_NODE_BINARY_URL,
    xray_install_url: str = XRAY_INSTALL_SCRIPT_URL,
    sing_box_install_url: str = SING_BOX_INSTALL_SCRIPT_URL,
) -> ProvisionNodeResult:
    specs = payload.inbounds
    core_kind = choose_core_kind([inbound.protocol for inbound in payload.inbounds])
    current_config_provider = current_config_provider or (lambda: current_config)
    validate_install_sources(
        core_kind,
        binary_url=binary_url,
        xray_install_url=xray_install_url,
        sing_box_install_url=sing_box_install_url,
    )

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

    with _config_apply_lock:
        current_config = current_config_provider()
        validate_requested_port_conflicts(payload, core_kind, current_config, specs)
        generated_inbounds = build_generated_inbounds(dbnode.id, specs)
        validate_generated_inbound_conflicts(current_config, generated_inbounds)
        active_tags = [inbound["tag"] for inbound in generated_inbounds]

        candidate_config = deepcopy(current_config)
        candidate_config.setdefault("inbounds", [])
        candidate_config["inbounds"].extend(generated_inbounds)
        XRayConfig(candidate_config)

        inbound_rows = []
        for inbound, inbound_spec in zip(generated_inbounds, payload.inbounds):
            inbound_row = ProxyInbound(tag=inbound["tag"], owner_node_id=dbnode.id)
            is_sing_box_tls = inbound_spec.protocol in SING_BOX_ONLY_PROVISION_PROTOCOLS
            is_hy2 = inbound_spec.protocol == NodeProvisionProtocol.hy2
            db.add(inbound_row)
            db.add(
                DBProxyHost(
                    remark=f"{payload.name} ({{USERNAME}}) [{{PROTOCOL}} - {{TRANSPORT}}]",
                    address=payload.address,
                    port=inbound_spec.port,
                    allowinsecure=True if is_sing_box_tls else None,
                    alpn="h3" if is_hy2 else None,
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
            "SING_BOX_INSTALL_SCRIPT_URL must be configured for sing-box node install"
        )


def validate_core_config_preserves_panel_inbounds(db: Session, payload: dict) -> None:
    requested_tags = {
        inbound.get("tag")
        for inbound in payload.get("inbounds", [])
        if inbound.get("tag")
    }
    managed_tags = _panel_managed_inbound_tags(db)
    missing_tags = sorted(managed_tags - requested_tags)
    if missing_tags:
        raise ValueError(
            "Core config is missing panel-managed inbound(s): "
            + ", ".join(missing_tags)
        )


def apply_core_config_update(db: Session, payload: dict) -> dict:
    with _config_apply_lock:
        validate_core_config_preserves_panel_inbounds(db, payload)
        payload, _ = cleanup_orphaned_provisioned_inbounds(db, payload)
        XRayConfig(payload, api_port=xray.config.api_port)
        _apply_provisioned_config(payload)
        return payload


def cleanup_orphaned_provisioned_inbounds(
    db: Session, payload: dict
) -> tuple[dict, list[str]]:
    known_tags = {
        tag
        for (tag,) in db.query(ProxyInbound.tag).all()
        if tag
    }
    cleaned = deepcopy(payload)
    removed_tags = []
    retained_inbounds = []
    for inbound in cleaned.get("inbounds", []):
        tag = inbound.get("tag")
        if (
            tag
            and _GENERATED_INBOUND_TAG.match(tag)
            and tag not in known_tags
        ):
            removed_tags.append(tag)
            continue
        retained_inbounds.append(inbound)

    cleaned["inbounds"] = retained_inbounds
    return cleaned, removed_tags


def reconcile_orphaned_provisioned_config(db: Session) -> list[str]:
    with _config_apply_lock:
        cleaned, removed_tags = cleanup_orphaned_provisioned_inbounds(db, xray.config.copy())
        if not removed_tags:
            return []
        xray.config = XRayConfig(cleaned, api_port=xray.config.api_port)
        _atomic_write_text(XRAY_JSON, json.dumps(cleaned, indent=4))
        xray.hosts.update()
        return removed_tags


def generated_inbound_tags_for_node(db: Session, dbnode: DBNode) -> list[str]:
    return [
        tag
        for (tag,) in db.query(ProxyInbound.tag)
        .filter(ProxyInbound.owner_node_id == dbnode.id)
        .order_by(ProxyInbound.tag)
        .all()
        if tag
    ]


def remove_provisioned_node(db: Session, dbnode: DBNode) -> list[str]:
    tags = generated_inbound_tags_for_node(db, dbnode)
    with _config_apply_lock:
        crud.remove_node(db, dbnode, remove_inbound_tags=tags)
        if tags:
            payload = xray.config.copy()
            payload["inbounds"] = [
                inbound
                for inbound in payload.get("inbounds", [])
                if inbound.get("tag") not in set(tags)
            ]
            _apply_provisioned_config(payload)
    return tags


def _panel_managed_inbound_tags(db: Session) -> set[str]:
    return {
        tag
        for (tag,) in db.query(ProxyInbound.tag)
        .filter(ProxyInbound.owner_node_id.isnot(None))
        .all()
        if tag
    }


def validate_generated_inbound_conflicts(
    current_config: dict, generated_inbounds: Sequence[dict]
) -> None:
    occupied = {}
    for inbound in current_config.get("inbounds", []):
        for endpoint in _inbound_endpoints(inbound):
            occupied.setdefault(endpoint, inbound.get("tag", "<existing>"))

    generated_seen = {}
    for inbound in generated_inbounds:
        for endpoint in _inbound_endpoints(inbound):
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
    payload: NodeProvisionCreate,
    core_kind: str,
    current_config: dict,
    specs: Sequence[ProvisionInboundSpec],
) -> None:
    occupied = {}
    for inbound in current_config.get("inbounds", []):
        for endpoint in _inbound_endpoints(inbound):
            occupied.setdefault(endpoint, inbound.get("tag", "<existing>"))

    generated_seen = {}
    for protocol, port, _ in [_normalize_inbound_spec(spec) for spec in specs]:
        if port == payload.port:
            raise ValueError(
                f"Generated inbound service port conflict: {protocol.value}:{port} conflicts with node service port"
            )
        if core_kind == "xray" and port == payload.api_port:
            raise ValueError(
                f"Generated inbound api port conflict: {protocol.value}:{port} conflicts with Xray API port"
            )
        endpoint = (_normalize_bind("0.0.0.0"), port, _protocol_transport(protocol))
        label = f"{protocol.value}:{port}"
        seen_conflict = _find_endpoint_conflict(endpoint, generated_seen)
        occupied_conflict = _find_endpoint_conflict(endpoint, occupied)
        if seen_conflict:
            raise ValueError(
                f"Generated inbound port conflict: {label} conflicts with {seen_conflict}"
            )
        if occupied_conflict:
            raise ValueError(
                f"Generated inbound port conflict: {label} conflicts with {occupied_conflict}"
            )
        generated_seen[endpoint] = label


def _inbound_endpoints(inbound: dict) -> list[tuple[str, int, str]]:
    port = inbound.get("port")
    if port is None:
        return []
    try:
        normalized_port = int(port)
    except (TypeError, ValueError):
        return []

    listen = inbound.get("listen") or "0.0.0.0"
    return [
        (_normalize_bind(listen), normalized_port, transport)
        for transport in _inbound_transports(inbound)
    ]


def _protocol_transport(protocol: NodeProvisionProtocol) -> str:
    return "udp" if protocol == NodeProvisionProtocol.hy2 else "tcp"


def _find_endpoint_conflict(
    endpoint: tuple[str, int, str], endpoints: dict[tuple[str, int, str], str]
) -> str | None:
    bind, port, transport = endpoint
    for existing_endpoint, label in endpoints.items():
        existing_bind, existing_port, existing_transport = existing_endpoint
        if (
            existing_port == port
            and existing_transport == transport
            and _binds_conflict(existing_bind, bind)
        ):
            return label
    return None


def _normalize_bind(bind: str) -> str:
    return "" if bind in {"0.0.0.0", "::", ""} else bind


def _binds_conflict(left: str, right: str) -> bool:
    left = _normalize_bind(left)
    right = _normalize_bind(right)
    return left == right or left == "" or right == ""


def _inbound_transports(inbound: dict) -> set[str]:
    stream_settings = inbound.get("streamSettings") or {}
    network = stream_settings.get("network")
    if network in {"hysteria", "hysteria2", "quic"}:
        return {"udp"}
    settings = inbound.get("settings") or {}
    settings_network = settings.get("network")
    if isinstance(settings_network, str):
        transports = {
            item.strip()
            for item in settings_network.split(",")
            if item.strip() in {"tcp", "udp"}
        }
        if transports:
            return transports
    return {"tcp"}


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
    consume: bool = True,
) -> NodeInstallPayload | None:
    record = crud.redeem_node_provision_token(db, token) if consume else crud.get_node_provision_token(db, token)
    if not record:
        return None

    tls = crud.get_tls_certificate(db)
    node = record.node
    if node is None:
        return None
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
  --data "{{\\"token\\":\\"$TOKEN\\",\\"consume\\":false}}")"

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

for attempt in $(seq 1 10); do
  if systemctl is-active --quiet marzban-node; then
    sleep 1
    if systemctl is-active --quiet marzban-node; then
      break
    fi
  fi
  if [ "$attempt" -eq 10 ]; then
    systemctl status marzban-node --no-pager >&2 || true
    exit 1
  fi
  sleep 1
done
"""
