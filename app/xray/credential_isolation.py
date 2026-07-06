from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from hashlib import sha256

from app import xray
from app.models.proxy import ProxySettings, ProxyTypes
from app.models.user import UserStatus

RUNNABLE_STATUSES = {UserStatus.active, UserStatus.on_hold}


@dataclass(frozen=True, order=True)
class CredentialKey:
    protocol: str
    inbound_tag: str
    credential: str = field(repr=False)


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
    settings = proxy.settings if isinstance(proxy.settings, dict) else {}
    credential = _credential_for_proxy(proxy_type, settings)
    if credential is None:
        return ()
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


def repair_duplicate_credentials(users) -> list[str]:
    repaired_usernames: list[str] = []
    users_by_username = {user.username: user for user in users}

    while duplicates := tuple(find_duplicate_credentials(users)):
        duplicate = duplicates[0]
        keeper = duplicate.users[0]
        repaired_this_round = False

        for username in duplicate.users:
            if username == keeper:
                continue
            user = users_by_username[username]
            for proxy in user.proxies:
                if duplicate.key not in credential_keys_for_proxy(proxy):
                    continue
                _rotate_proxy_credential(proxy)
                repaired_usernames.append(username)
                repaired_this_round = True
                break

        if not repaired_this_round:
            raise RuntimeError(
                "Unable to repair duplicate proxy credentials for "
                f"{duplicate.key.protocol} inbound {duplicate.key.inbound_tag}"
            )
        if tuple(find_duplicate_credentials(users)) == duplicates:
            raise RuntimeError(
                "Unable to repair duplicate proxy credentials for "
                f"{duplicate.key.protocol} inbound {duplicate.key.inbound_tag}"
            )

    return repaired_usernames


def _rotate_proxy_credential(proxy) -> None:
    settings = proxy.settings if isinstance(proxy.settings, dict) else {}
    settings_model = ProxySettings.from_dict(ProxyTypes(proxy.type), settings)
    settings_model.revoke()
    proxy.settings = settings_model.dict(no_obj=True)


def _credential_for_proxy(proxy_type: ProxyTypes, settings: dict) -> str | None:
    if proxy_type in {ProxyTypes.VMess, ProxyTypes.VLESS}:
        credential = settings.get("id")
        return str(credential) if credential is not None else None
    if proxy_type in {ProxyTypes.Trojan, ProxyTypes.AnyTLS}:
        credential = settings.get("password")
        return str(credential) if credential is not None else None
    if proxy_type == ProxyTypes.Hysteria:
        credential = settings.get("auth")
        return str(credential) if credential is not None else None
    if proxy_type == ProxyTypes.Shadowsocks:
        method = settings.get("method")
        password = settings.get("password")
        if method is None or password is None:
            return None
        return f"{method}:{password}"
    raise ValueError(f"unsupported proxy type: {proxy_type}")
