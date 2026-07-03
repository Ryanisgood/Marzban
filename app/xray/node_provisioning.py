import hashlib
import hmac
from typing import Iterable, Sequence, Tuple

from app.models.node_provision import NodeProvisionProtocol


ProtocolPort = Tuple[NodeProvisionProtocol, int]


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
