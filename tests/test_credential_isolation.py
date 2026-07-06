import os
from datetime import datetime

os.environ.setdefault("XRAY_EXECUTABLE_PATH", "/bin/echo")

import pytest

from app import xray
from app.db.models import Proxy, ProxyInbound, User as DBUser
from app.models.proxy import ProxyTypes
from app.models.user import UserDataLimitResetStrategy, UserStatus
from app.xray.config import XRayConfig
from app.xray.credential_isolation import (
    CredentialDuplicate,
    CredentialKey,
    credential_keys_for_user,
    find_duplicate_credentials,
    repair_duplicate_credentials,
)


def _config():
    return XRayConfig(
        {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {"tag": "VMess", "protocol": "vmess", "port": 1001},
                {"tag": "VLESS", "protocol": "vless", "port": 1002},
                {"tag": "Trojan", "protocol": "trojan", "port": 1003},
                {
                    "tag": "SS-A",
                    "protocol": "shadowsocks",
                    "port": 1004,
                    "settings": {"clients": []},
                },
                {
                    "tag": "SS-B",
                    "protocol": "shadowsocks",
                    "port": 1005,
                    "settings": {"clients": []},
                },
                {
                    "tag": "HY2",
                    "protocol": "hysteria",
                    "port": 1006,
                    "settings": {"version": 2, "users": []},
                    "streamSettings": {"network": "hysteria"},
                },
                {
                    "tag": "AnyTLS",
                    "protocol": "anytls",
                    "port": 1007,
                    "settings": {"users": []},
                    "streamSettings": {
                        "network": "tcp",
                        "security": "tls",
                        "tlsSettings": {},
                    },
                },
            ],
            "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
        }
    )


def _user(user_id, username, status=UserStatus.active, proxies=None):
    dbuser = DBUser(
        id=user_id,
        username=username,
        status=status,
        used_traffic=0,
        data_limit_reset_strategy=UserDataLimitResetStrategy.no_reset,
        created_at=datetime.utcnow(),
        proxies=proxies or [],
    )
    for proxy in dbuser.proxies:
        proxy.user = dbuser
    return dbuser


def _proxy(proxy_type, settings, excluded=()):
    proxy = Proxy(type=proxy_type, settings=settings)
    proxy.excluded_inbounds = [ProxyInbound(tag=tag) for tag in excluded]
    return proxy


def test_credential_keys_include_effective_inbound_and_protocol_credentials(
    monkeypatch,
):
    monkeypatch.setattr(xray, "config", _config())
    user = _user(
        1,
        "alice",
        proxies=[
            _proxy(
                ProxyTypes.VMess,
                {"id": "11111111-1111-1111-1111-111111111111"},
            ),
            _proxy(
                ProxyTypes.VLESS,
                {"id": "22222222-2222-2222-2222-222222222222"},
            ),
            _proxy(ProxyTypes.Trojan, {"password": "trojan-secret"}),
            _proxy(
                ProxyTypes.Shadowsocks,
                {"method": "chacha20-ietf-poly1305", "password": "ss-secret"},
                excluded=("SS-B",),
            ),
            _proxy(ProxyTypes.Hysteria, {"auth": "hy2-secret"}),
            _proxy(ProxyTypes.AnyTLS, {"password": "anytls-secret"}),
        ],
    )

    keys = set(credential_keys_for_user(user))

    assert (
        CredentialKey(
            "vmess", "VMess", "11111111-1111-1111-1111-111111111111"
        )
        in keys
    )
    assert (
        CredentialKey(
            "vless", "VLESS", "22222222-2222-2222-2222-222222222222"
        )
        in keys
    )
    assert CredentialKey("trojan", "Trojan", "trojan-secret") in keys
    assert (
        CredentialKey(
            "shadowsocks", "SS-A", "chacha20-ietf-poly1305:ss-secret"
        )
        in keys
    )
    assert (
        CredentialKey(
            "shadowsocks", "SS-B", "chacha20-ietf-poly1305:ss-secret"
        )
        not in keys
    )
    assert CredentialKey("hysteria", "HY2", "hy2-secret") in keys
    assert CredentialKey("anytls", "AnyTLS", "anytls-secret") in keys


def test_duplicate_detection_is_runnable_and_inbound_scoped(monkeypatch):
    monkeypatch.setattr(xray, "config", _config())
    alice = _user(
        1,
        "alice",
        proxies=[
            _proxy(ProxyTypes.Hysteria, {"auth": "shared"}, excluded=())
        ],
    )
    bob = _user(
        2,
        "bob",
        proxies=[
            _proxy(ProxyTypes.Hysteria, {"auth": "shared"}, excluded=())
        ],
    )
    charlie = _user(
        3,
        "charlie",
        status=UserStatus.disabled,
        proxies=[
            _proxy(ProxyTypes.Hysteria, {"auth": "shared"}, excluded=())
        ],
    )

    duplicates = find_duplicate_credentials([alice, bob, charlie])

    assert duplicates == [
        CredentialDuplicate(
            key=CredentialKey("hysteria", "HY2", "shared"),
            users=("alice", "bob"),
        )
    ]


def test_same_secret_on_disjoint_inbounds_is_allowed(monkeypatch):
    monkeypatch.setattr(xray, "config", _config())
    alice = _user(
        1,
        "alice",
        proxies=[
            _proxy(
                ProxyTypes.Shadowsocks,
                {"method": "chacha20-ietf-poly1305", "password": "same"},
                excluded=("SS-B",),
            )
        ],
    )
    bob = _user(
        2,
        "bob",
        proxies=[
            _proxy(
                ProxyTypes.Shadowsocks,
                {"method": "chacha20-ietf-poly1305", "password": "same"},
                excluded=("SS-A",),
            )
        ],
    )

    assert find_duplicate_credentials([alice, bob]) == []


def test_duplicate_repr_does_not_include_raw_credential(monkeypatch):
    monkeypatch.setattr(xray, "config", _config())
    alice = _user(
        1,
        "alice",
        proxies=[_proxy(ProxyTypes.Hysteria, {"auth": "leaky-secret"})],
    )
    bob = _user(
        2,
        "bob",
        proxies=[_proxy(ProxyTypes.Hysteria, {"auth": "leaky-secret"})],
    )

    duplicate = find_duplicate_credentials([alice, bob])[0]

    assert duplicate == CredentialDuplicate(
        key=CredentialKey("hysteria", "HY2", "leaky-secret"),
        users=("alice", "bob"),
    )
    assert "leaky-secret" not in repr(duplicate)


@pytest.mark.parametrize(
    "proxy_type,settings",
    [
        (ProxyTypes.VMess, {}),
        (ProxyTypes.VLESS, {}),
        (ProxyTypes.Trojan, {}),
        (ProxyTypes.AnyTLS, {}),
        (ProxyTypes.AnyTLS, None),
        (ProxyTypes.AnyTLS, []),
        (ProxyTypes.Hysteria, {}),
        (ProxyTypes.Shadowsocks, {"method": "chacha20-ietf-poly1305"}),
        (ProxyTypes.Shadowsocks, {"password": "missing-method"}),
    ],
)
def test_malformed_proxy_credentials_are_skipped(
    monkeypatch, proxy_type, settings
):
    monkeypatch.setattr(xray, "config", _config())
    user = _user(1, "alice", proxies=[_proxy(proxy_type, settings)])

    assert credential_keys_for_user(user) == ()
    assert find_duplicate_credentials([user]) == []


def test_repair_duplicate_credentials_rotates_all_but_one_user(monkeypatch):
    monkeypatch.setattr(xray, "config", _config())
    alice = _user(
        1, "alice", proxies=[_proxy(ProxyTypes.AnyTLS, {"password": "same"})]
    )
    bob = _user(
        2, "bob", proxies=[_proxy(ProxyTypes.AnyTLS, {"password": "same"})]
    )

    repaired = repair_duplicate_credentials([alice, bob])

    assert repaired == ["bob"]
    assert alice.proxies[0].settings["password"] == "same"
    assert bob.proxies[0].settings["password"] != "same"
    assert find_duplicate_credentials([alice, bob]) == []
