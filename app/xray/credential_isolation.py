from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256

from app import xray
from app.models.proxy import ProxyTypes
from app.models.user import UserStatus

RUNNABLE_STATUSES = {UserStatus.active, UserStatus.on_hold}


@dataclass(frozen=True, order=True)
class CredentialKey:
    protocol: str
    inbound_tag: str
    credential: str


@dataclass(frozen=True)
class CredentialDuplicate:
    key: CredentialKey
    users: tuple[str, ...]


def credential_fingerprint(credential) -> str:
    return sha256(str(credential).encode()).hexdigest()[:12]


def effective_inbound_tags_for_proxy(proxy) -> tuple[str, ...]:
    proxy_type = ProxyTypes(proxy.type)
    excluded_tags = {inbound.tag for inbound in proxy.excluded_inbounds}
    return tuple(
        inbound["tag"]
        for inbound in xray.config.inbounds_by_protocol.get(proxy_type, [])
        if inbound["tag"] not in excluded_tags
    )


def credential_keys_for_proxy(proxy) -> tuple[CredentialKey, ...]:
    proxy_type = ProxyTypes(proxy.type)
    credential = _credential_for_proxy(proxy_type, proxy.settings)
    return tuple(
        CredentialKey(proxy_type.value, inbound_tag, credential)
        for inbound_tag in effective_inbound_tags_for_proxy(proxy)
    )


def credential_keys_for_user(user) -> tuple[CredentialKey, ...]:
    return tuple(
        key for proxy in user.proxies for key in credential_keys_for_proxy(proxy)
    )


def find_duplicate_credentials(users) -> list[CredentialDuplicate]:
    users_by_key = defaultdict(set)
    for user in users:
        if user.status not in RUNNABLE_STATUSES:
            continue
        for key in credential_keys_for_user(user):
            users_by_key[key].add(user.username)

    return [
        CredentialDuplicate(key=key, users=tuple(sorted(usernames)))
        for key, usernames in sorted(users_by_key.items())
        if len(usernames) > 1
    ]


def _credential_for_proxy(proxy_type: ProxyTypes, settings: dict) -> str:
    if proxy_type in {ProxyTypes.VMess, ProxyTypes.VLESS}:
        return str(settings["id"])
    if proxy_type in {ProxyTypes.Trojan, ProxyTypes.AnyTLS}:
        return str(settings["password"])
    if proxy_type == ProxyTypes.Hysteria:
        return str(settings["auth"])
    if proxy_type == ProxyTypes.Shadowsocks:
        return f"{settings['method']}:{settings['password']}"
    raise ValueError(f"unsupported proxy type: {proxy_type}")
