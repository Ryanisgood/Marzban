import os
import urllib.parse
from datetime import datetime
from importlib import import_module
from ipaddress import IPv4Address
from types import SimpleNamespace

os.environ.setdefault("XRAY_EXECUTABLE_PATH", "/bin/echo")

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app import xray
from app.db.models import Proxy, User as DBUser
import app.models.user as user_models
from app.models.proxy import AnyTLSSettings, HysteriaSettings, ProxyTypes
from app.models.user import UserCreate, UserDataLimitResetStrategy, UserStatus
from app.subscription.clash import ClashMetaConfiguration
from app.subscription.v2ray import V2rayShareLink
from app.utils.crypto import get_cert_SANs
from app.xray import operations
from app.xray.config import XRayConfig
from xray_api.types.account import HysteriaAccount

xray_config_module = import_module("app.xray.config")


def _base_config(inbound):
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [inbound],
        "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
    }


def _hysteria_inbound():
    return {
        "tag": "HY2",
        "protocol": "hysteria",
        "port": 443,
        "settings": {"version": 2, "users": []},
        "streamSettings": {"network": "hysteria"},
    }


def _hysteria_inbound_with_tag(tag):
    inbound = _hysteria_inbound()
    inbound["tag"] = tag
    return inbound


def _anytls_inbound():
    return {
        "tag": "AnyTLS",
        "protocol": "anytls",
        "port": 9443,
        "settings": {"users": []},
        "streamSettings": {
            "network": "tcp",
            "security": "tls",
            "tlsSettings": {"serverName": "any.example.com"},
        },
    }


def _dbuser_with_hysteria(auth="secret-auth"):
    return DBUser(
        id=7,
        username="alice",
        status=UserStatus.active,
        used_traffic=0,
        data_limit_reset_strategy=UserDataLimitResetStrategy.no_reset,
        created_at=datetime.utcnow(),
        proxies=[Proxy(type=ProxyTypes.Hysteria, settings={"auth": auth})],
    )


def _dbuser_with_anytls(password="secret-password"):
    return DBUser(
        id=8,
        username="bob",
        status=UserStatus.active,
        used_traffic=0,
        data_limit_reset_strategy=UserDataLimitResetStrategy.no_reset,
        created_at=datetime.utcnow(),
        proxies=[Proxy(type=ProxyTypes.AnyTLS, settings={"password": password})],
    )


def test_hysteria_settings_create_account_and_revoke_auth():
    settings = HysteriaSettings(auth="first-secret")

    account = ProxyTypes.Hysteria.account_model(email="1.alice", **settings.dict(no_obj=True))

    assert account.email == "1.alice"
    assert account.message.type.endswith("xray.proxy.hysteria.account.Account")
    assert account.message.value

    settings.revoke()

    assert settings.auth
    assert settings.auth != "first-secret"


def test_anytls_settings_create_and_revoke_password():
    settings = AnyTLSSettings(password="first-secret")

    assert settings.password == "first-secret"

    settings.revoke()

    assert settings.password
    assert settings.password != "first-secret"


def test_xray_config_recognizes_hysteria2_inbound_only():
    config = XRayConfig(
        _base_config(
            {
                "tag": "HY2",
                "protocol": "hysteria",
                "port": 443,
                "settings": {"version": 2, "users": []},
                "streamSettings": {
                    "network": "hysteria",
                    "security": "tls",
                    "tlsSettings": {
                        "serverName": "hy.example.com",
                        "alpn": ["h3"],
                    },
                },
            }
        )
    )

    assert config.inbounds_by_protocol[ProxyTypes.Hysteria][0]["tag"] == "HY2"
    assert config.inbounds_by_tag["HY2"]["network"] == "hysteria"
    assert config.inbounds_by_tag["HY2"]["tls"] == "tls"
    assert config.inbounds_by_tag["HY2"]["sni"] == ["hy.example.com"]
    assert config.inbounds_by_tag["HY2"]["alpn"] == "h3"
    assert config.get_inbound("HY2")["settings"]["users"] == []


def test_xray_config_recognizes_anytls_inbound():
    config = XRayConfig(_base_config(_anytls_inbound()))

    assert config.inbounds_by_protocol[ProxyTypes.AnyTLS][0]["tag"] == "AnyTLS"
    assert config.inbounds_by_tag["AnyTLS"]["network"] == "tcp"
    assert config.inbounds_by_tag["AnyTLS"]["tls"] == "tls"
    assert config.inbounds_by_tag["AnyTLS"]["sni"] == ["any.example.com"]
    assert config.get_inbound("AnyTLS")["settings"]["users"] == []


def test_xray_core_config_filters_anytls_inbound_from_main_xray():
    config = XRayConfig(_base_config(_anytls_inbound()))

    xray_config = config.xray_core_config()

    assert "AnyTLS" in config.inbounds_by_tag
    assert xray_config.get_inbound("AnyTLS") is None
    assert "AnyTLS" not in xray_config.inbounds_by_tag


def test_xray_config_ignores_hysteria_v1_inbound():
    config = XRayConfig(
        _base_config(
            {
                "tag": "HY1",
                "protocol": "hysteria",
                "port": 443,
                "settings": {"version": 1, "clients": []},
            }
        )
    )

    assert ProxyTypes.Hysteria not in config.inbounds_by_protocol
    assert "HY1" not in config.inbounds_by_tag


def test_xray_config_ignores_hysteria2_without_hysteria_transport():
    config = XRayConfig(
        _base_config(
            {
                "tag": "HY2",
                "protocol": "hysteria",
                "port": 443,
                "settings": {"version": 2, "users": []},
                "streamSettings": {"network": "tcp"},
            }
        )
    )

    assert ProxyTypes.Hysteria not in config.inbounds_by_protocol
    assert "HY2" not in config.inbounds_by_tag


def test_user_create_accepts_hysteria_proxy_and_inbound(monkeypatch):
    config = XRayConfig(_base_config(_hysteria_inbound()))
    monkeypatch.setattr(xray, "config", config)

    user = UserCreate(
        username="alice",
        proxies={"hysteria": {"auth": "secret-auth"}},
        inbounds={"hysteria": ["HY2"]},
    )

    assert ProxyTypes.Hysteria in user.proxies
    assert user.proxies[ProxyTypes.Hysteria].auth == "secret-auth"
    assert user.inbounds[ProxyTypes.Hysteria] == ["HY2"]


def test_user_create_accepts_anytls_proxy_and_inbound(monkeypatch):
    config = XRayConfig(_base_config(_anytls_inbound()))
    monkeypatch.setattr(xray, "config", config)

    user = UserCreate(
        username="bob",
        proxies={"anytls": {"password": "secret-password"}},
        inbounds={"anytls": ["AnyTLS"]},
    )

    assert ProxyTypes.AnyTLS in user.proxies
    assert user.proxies[ProxyTypes.AnyTLS].password == "secret-password"
    assert user.inbounds[ProxyTypes.AnyTLS] == ["AnyTLS"]


def test_include_db_users_writes_anytls_users(monkeypatch):
    config = XRayConfig(_base_config(_anytls_inbound()))
    dbuser = _dbuser_with_anytls()

    class Query:
        def join(self, *args, **kwargs):
            return self

        def outerjoin(self, *args, **kwargs):
            return self

        def filter(self, *args, **kwargs):
            return self

        def group_by(self, *args, **kwargs):
            return self

        def all(self):
            return [
                SimpleNamespace(
                    id=dbuser.id,
                    username=dbuser.username,
                    type=ProxyTypes.AnyTLS.value,
                    settings={"password": "secret-password"},
                    excluded_inbound_tags=None,
                )
            ]

    class DB:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def query(self, *args, **kwargs):
            return Query()

    monkeypatch.setattr(xray_config_module, "GetDB", lambda: DB())

    generated = config.include_db_users()

    users = generated.get_inbound("AnyTLS")["settings"]["users"]
    assert users == [{"email": "8.bob", "password": "secret-password"}]


def test_hysteria_user_lifecycle_updates_xray_api(monkeypatch):
    config = XRayConfig(_base_config(_hysteria_inbound()))
    calls = []

    monkeypatch.setattr(xray, "config", config)
    monkeypatch.setattr(xray, "api", object())
    monkeypatch.setattr(xray, "nodes", {})
    monkeypatch.setattr(user_models, "generate_v2ray_links", lambda *args, **kwargs: [])
    monkeypatch.setattr(user_models, "create_subscription_token", lambda username: "token")
    monkeypatch.setattr(
        operations,
        "_add_user_to_inbound",
        lambda api, inbound_tag, account: calls.append(("add", inbound_tag, account)),
    )
    monkeypatch.setattr(
        operations,
        "_remove_user_from_inbound",
        lambda api, inbound_tag, email: calls.append(("remove", inbound_tag, email)),
    )

    dbuser = _dbuser_with_hysteria()

    operations.add_user(dbuser)
    operations.remove_user(dbuser)

    assert calls[0][0] == "add"
    assert calls[0][1] == "HY2"
    assert isinstance(calls[0][2], HysteriaAccount)
    assert calls[0][2].email == "7.alice"
    assert calls[0][2].auth == "secret-auth"
    assert calls[1] == ("remove", "HY2", "7.alice")


def test_hysteria_revoke_updates_xray_api_with_new_auth(monkeypatch):
    config = XRayConfig(_base_config(_hysteria_inbound()))
    calls = []

    monkeypatch.setattr(xray, "config", config)
    monkeypatch.setattr(xray, "api", object())
    monkeypatch.setattr(xray, "nodes", {})
    monkeypatch.setattr(user_models, "generate_v2ray_links", lambda *args, **kwargs: [])
    monkeypatch.setattr(user_models, "create_subscription_token", lambda username: "token")
    monkeypatch.setattr(
        operations,
        "_alter_inbound_user",
        lambda api, inbound_tag, account: calls.append(("alter", inbound_tag, account)),
    )

    operations.update_user(_dbuser_with_hysteria(auth="new-secret"))

    assert calls[0][0] == "alter"
    assert calls[0][1] == "HY2"
    assert isinstance(calls[0][2], HysteriaAccount)
    assert calls[0][2].email == "7.alice"
    assert calls[0][2].auth == "new-secret"


def test_hysteria_user_changes_restart_started_nodes_for_sing_box(monkeypatch):
    config = XRayConfig(_base_config(_hysteria_inbound()))
    restarted = []

    class HysteriaNode:
        connected = True
        started = True
        active_inbounds = ["HY2"]
        api = object()

    class VlessOnlyNode:
        connected = True
        started = True
        active_inbounds = ["VLESS"]
        api = object()

    monkeypatch.setattr(xray, "config", config)
    monkeypatch.setattr(xray, "api", object())
    monkeypatch.setattr(xray, "nodes", {42: HysteriaNode(), 43: VlessOnlyNode()})
    monkeypatch.setattr(user_models, "generate_v2ray_links", lambda *args, **kwargs: [])
    monkeypatch.setattr(user_models, "create_subscription_token", lambda username: "token")
    monkeypatch.setattr(operations, "_add_user_to_inbound", lambda *args, **kwargs: None)
    monkeypatch.setattr(operations, "_remove_user_from_inbound", lambda *args, **kwargs: None)
    monkeypatch.setattr(operations, "_alter_inbound_user", lambda *args, **kwargs: None)
    monkeypatch.setattr(operations, "restart_node", lambda node_id: restarted.append(node_id))

    dbuser = _dbuser_with_hysteria()
    operations.add_user(dbuser)
    operations.update_user(_dbuser_with_hysteria(auth="new-secret"))
    operations.remove_user(dbuser)

    assert restarted == [42, 42, 42]


def test_anytls_user_changes_restart_started_nodes_without_xray_api(monkeypatch):
    config = XRayConfig(_base_config(_anytls_inbound()))
    restarted = []
    api_calls = []

    class AnyTLSNode:
        connected = True
        started = True
        active_inbounds = ["AnyTLS"]

    class OtherNode:
        connected = True
        started = True
        active_inbounds = ["VLESS"]

    monkeypatch.setattr(xray, "config", config)
    monkeypatch.setattr(xray, "api", object())
    monkeypatch.setattr(xray, "nodes", {42: AnyTLSNode(), 43: OtherNode()})
    monkeypatch.setattr(user_models, "generate_v2ray_links", lambda *args, **kwargs: [])
    monkeypatch.setattr(user_models, "create_subscription_token", lambda username: "token")
    monkeypatch.setattr(
        operations,
        "_add_user_to_inbound",
        lambda *args, **kwargs: api_calls.append("add"),
    )
    monkeypatch.setattr(
        operations,
        "_alter_inbound_user",
        lambda *args, **kwargs: api_calls.append("alter"),
    )
    monkeypatch.setattr(
        operations,
        "_remove_user_from_inbound",
        lambda *args, **kwargs: api_calls.append("remove"),
    )
    monkeypatch.setattr(operations, "restart_node", lambda node_id: restarted.append(node_id))

    dbuser = _dbuser_with_anytls()
    operations.add_user(dbuser)
    operations.update_user(_dbuser_with_anytls(password="new-secret"))
    operations.remove_user(dbuser)

    assert api_calls == []
    assert restarted == [42, 42, 42]


def test_hysteria_user_migration_restarts_old_and_new_inbound_nodes(monkeypatch):
    config = XRayConfig(
        {
            "log": {"loglevel": "warning"},
            "inbounds": [
                _hysteria_inbound_with_tag("HY2_A"),
                _hysteria_inbound_with_tag("HY2_B"),
                _hysteria_inbound_with_tag("HY2_C"),
            ],
            "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
        }
    )
    restarted = []

    class NodeA:
        connected = True
        started = True
        active_inbounds = ["HY2_A"]
        api = object()

    class NodeB:
        connected = True
        started = True
        active_inbounds = ["HY2_B"]
        api = object()

    class NodeC:
        connected = True
        started = True
        active_inbounds = ["HY2_C"]
        api = object()

    monkeypatch.setattr(xray, "config", config)
    monkeypatch.setattr(xray, "api", object())
    monkeypatch.setattr(xray, "nodes", {1: NodeA(), 2: NodeB(), 3: NodeC()})
    monkeypatch.setattr(user_models, "generate_v2ray_links", lambda *args, **kwargs: [])
    monkeypatch.setattr(user_models, "create_subscription_token", lambda username: "token")
    monkeypatch.setattr(operations, "_alter_inbound_user", lambda *args, **kwargs: None)
    monkeypatch.setattr(operations, "_remove_user_from_inbound", lambda *args, **kwargs: None)
    monkeypatch.setattr(operations, "restart_node", lambda node_id: restarted.append(node_id))

    operations.update_user(
        _dbuser_with_hysteria(auth="new-secret"),
        config_reload_inbounds={"HY2_A", "HY2_B"},
    )

    assert restarted == [1, 2]


def test_hysteria_user_removal_restarts_only_previous_inbound_nodes(monkeypatch):
    config = XRayConfig(
        {
            "log": {"loglevel": "warning"},
            "inbounds": [
                _hysteria_inbound_with_tag("HY2_A"),
                _hysteria_inbound_with_tag("HY2_B"),
            ],
            "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
        }
    )
    restarted = []

    class NodeA:
        connected = True
        started = True
        active_inbounds = ["HY2_A"]
        api = object()

    class NodeB:
        connected = True
        started = True
        active_inbounds = ["HY2_B"]
        api = object()

    monkeypatch.setattr(xray, "config", config)
    monkeypatch.setattr(xray, "api", object())
    monkeypatch.setattr(xray, "nodes", {1: NodeA(), 2: NodeB()})
    monkeypatch.setattr(operations, "_remove_user_from_inbound", lambda *args, **kwargs: None)
    monkeypatch.setattr(operations, "restart_node", lambda node_id: restarted.append(node_id))

    operations.remove_user(
        _dbuser_with_hysteria(),
        config_reload_inbounds={"HY2_A"},
    )

    assert restarted == [1]


def test_v2ray_share_link_generates_hysteria2_uri():
    link = V2rayShareLink.hysteria2(
        remark="Alice HY2",
        address="node.example.com",
        port=443,
        auth="secret auth",
        sni="hy.example.com",
        alpn="h3",
        ais=True,
    )

    parsed = urllib.parse.urlparse(link)
    query = urllib.parse.parse_qs(parsed.query)

    assert parsed.scheme == "hysteria2"
    assert urllib.parse.unquote(parsed.username) == "secret auth"
    assert parsed.hostname == "node.example.com"
    assert parsed.port == 443
    assert query["sni"] == ["hy.example.com"]
    assert query["alpn"] == ["h3"]
    assert query["insecure"] == ["1"]
    assert urllib.parse.unquote(parsed.fragment) == "Alice HY2"


def test_clash_meta_generates_hysteria2_proxy():
    conf = ClashMetaConfiguration()

    conf.add(
        remark="Alice HY2",
        address="node.example.com",
        inbound={
            "protocol": "hysteria",
            "port": 443,
            "network": "hysteria",
            "tls": "tls",
            "sni": "hy.example.com",
            "host": [],
            "path": "",
            "header_type": "",
            "alpn": "h3",
            "ais": True,
        },
        settings={"auth": "secret-auth"},
    )

    rendered = yaml.safe_load(conf.render())
    proxy = rendered["proxies"][0]

    assert proxy["name"] == "Alice HY2"
    assert proxy["type"] == "hysteria2"
    assert proxy["server"] == "node.example.com"
    assert proxy["port"] == 443
    assert proxy["password"] == "secret-auth"
    assert proxy["sni"] == "hy.example.com"
    assert proxy["alpn"] == ["h3"]
    assert proxy["skip-cert-verify"] is True


def test_certificate_sans_are_strings_for_subscription_generation():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Gozargah"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.utcnow())
        .not_valid_after(datetime.utcnow())
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("node.example.com"),
                x509.IPAddress(IPv4Address("192.0.2.10")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    sans = get_cert_SANs(cert.public_bytes(serialization.Encoding.PEM))

    assert sans == ["node.example.com", "192.0.2.10"]
    assert all(isinstance(san, str) for san in sans)
