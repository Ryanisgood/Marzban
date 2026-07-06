# Proxy Credential Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure deleted users cannot keep accessing nodes with already-downloaded configs by enforcing per-inbound unique credentials across all supported proxy protocols and fixing deletion paths that only remove database rows.

**Architecture:** Add a focused `app/xray/credential_isolation.py` module that computes runtime-equivalent inbound-scoped credential keys, detects duplicates, validates mutations, and repairs existing duplicate groups. Integrate it into CRUD write paths, CLI audit/repair commands, and bulk deletion runtime synchronization while keeping node runtime removal logic in `app/xray/operations.py`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Pydantic v2, Typer CLI, pytest; MarzbanX-node Rust/sing-box conversion is verified before write-time enforcement.

---

## File Structure

- Create `app/xray/credential_isolation.py`: credential key extraction, duplicate audit, validation, repair, and deletion-plan helpers.
- Create `tests/test_credential_isolation.py`: unit and CRUD-style tests for keys, duplicate detection, repair, and enforcement.
- Modify `app/db/crud.py`: call validation/repair helpers in create, update, revoke, status transition, and expose bulk deletion without losing runtime snapshots.
- Modify `app/routers/user.py`: route bulk expired deletion through a runtime removal plan before DB deletion.
- Modify `app/jobs/remove_expired_users.py`: run autodelete through the same runtime removal helper.
- Modify `cli/user.py`: add credential audit and repair commands.
- Modify `README.md`: document per-inbound credential uniqueness and repair impact.
- Potentially modify `/Users/zheng/Code/MarzbanX-node/src/xray_config.rs`: only if Shadowsocks top-level password is confirmed to be an accepted fallback credential with per-user `users`.

Use `/Users/zheng/Code/MarzbanX/.venv/bin/python -m pytest ...` for Python tests from `/private/tmp/MarzbanX-proxy-auth-isolation`.

## Task 1: Credential Key Extraction And Duplicate Audit

**Files:**
- Create: `app/xray/credential_isolation.py`
- Create: `tests/test_credential_isolation.py`

- [ ] **Step 1: Write failing tests for inbound-scoped credential keys**

Add this to `tests/test_credential_isolation.py`:

```python
from datetime import datetime
import pytest

from app import xray
from app.db.models import Proxy, ProxyInbound, User as DBUser
from app.models.proxy import ProxyTypes
from app.models.user import UserDataLimitResetStrategy, UserStatus
from app.xray.config import XRayConfig
from app.xray.credential_isolation import (
    CredentialKey,
    credential_keys_for_user,
    find_duplicate_credentials,
)


def _config():
    return XRayConfig(
        {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {"tag": "VMess", "protocol": "vmess", "port": 1001},
                {"tag": "VLESS", "protocol": "vless", "port": 1002},
                {"tag": "Trojan", "protocol": "trojan", "port": 1003},
                {"tag": "SS-A", "protocol": "shadowsocks", "port": 1004, "settings": {"clients": []}},
                {"tag": "SS-B", "protocol": "shadowsocks", "port": 1005, "settings": {"clients": []}},
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
                    "streamSettings": {"network": "tcp", "security": "tls", "tlsSettings": {}},
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


def test_credential_keys_include_effective_inbound_and_protocol_credentials(monkeypatch):
    monkeypatch.setattr(xray, "config", _config())
    user = _user(
        1,
        "alice",
        proxies=[
            _proxy(ProxyTypes.VMess, {"id": "11111111-1111-1111-1111-111111111111"}),
            _proxy(ProxyTypes.VLESS, {"id": "22222222-2222-2222-2222-222222222222"}),
            _proxy(ProxyTypes.Trojan, {"password": "trojan-secret"}),
            _proxy(ProxyTypes.Shadowsocks, {"method": "chacha20-ietf-poly1305", "password": "ss-secret"}, excluded=("SS-B",)),
            _proxy(ProxyTypes.Hysteria, {"auth": "hy2-secret"}),
            _proxy(ProxyTypes.AnyTLS, {"password": "anytls-secret"}),
        ],
    )

    keys = set(credential_keys_for_user(user))

    assert CredentialKey("vmess", "VMess", "11111111-1111-1111-1111-111111111111") in keys
    assert CredentialKey("vless", "VLESS", "22222222-2222-2222-2222-222222222222") in keys
    assert CredentialKey("trojan", "Trojan", "trojan-secret") in keys
    assert CredentialKey("shadowsocks", "SS-A", "chacha20-ietf-poly1305:ss-secret") in keys
    assert CredentialKey("shadowsocks", "SS-B", "chacha20-ietf-poly1305:ss-secret") not in keys
    assert CredentialKey("hysteria", "HY2", "hy2-secret") in keys
    assert CredentialKey("anytls", "AnyTLS", "anytls-secret") in keys
```

- [ ] **Step 2: Run test and verify it fails**

Run: `/Users/zheng/Code/MarzbanX/.venv/bin/python -m pytest tests/test_credential_isolation.py::test_credential_keys_include_effective_inbound_and_protocol_credentials -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.xray.credential_isolation'`.

- [ ] **Step 3: Implement minimal key extraction**

Create `app/xray/credential_isolation.py`:

```python
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
from typing import Iterable

from app import xray
from app.db.models import Proxy, User
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


def credential_fingerprint(credential: str) -> str:
    return hashlib.sha256(credential.encode()).hexdigest()[:12]


def _proxy_type_value(proxy_type) -> str:
    return ProxyTypes(proxy_type).value


def _credential_value(proxy: Proxy) -> str | None:
    proxy_type = ProxyTypes(proxy.type)
    settings = proxy.settings or {}
    if proxy_type in (ProxyTypes.VMess, ProxyTypes.VLESS):
        value = settings.get("id")
        return str(value) if value else None
    if proxy_type in (ProxyTypes.Trojan, ProxyTypes.AnyTLS):
        return settings.get("password")
    if proxy_type == ProxyTypes.Hysteria:
        return settings.get("auth")
    if proxy_type == ProxyTypes.Shadowsocks:
        method = settings.get("method")
        password = settings.get("password")
        return f"{method}:{password}" if method and password else None
    return None


def effective_inbound_tags_for_proxy(proxy: Proxy) -> list[str]:
    proxy_type = ProxyTypes(proxy.type)
    excluded = {inbound.tag for inbound in getattr(proxy, "excluded_inbounds", [])}
    return [
        inbound["tag"]
        for inbound in xray.config.inbounds_by_protocol.get(proxy_type, [])
        if inbound["tag"] not in excluded
    ]


def credential_keys_for_user(user: User) -> list[CredentialKey]:
    keys: list[CredentialKey] = []
    for proxy in user.proxies:
        keys.extend(credential_keys_for_proxy(proxy))
    return keys


def credential_keys_for_proxy(proxy: Proxy) -> list[CredentialKey]:
    credential = _credential_value(proxy)
    if not credential:
        return []
    protocol = _proxy_type_value(proxy.type)
    return [
        CredentialKey(protocol, inbound_tag, credential)
        for inbound_tag in effective_inbound_tags_for_proxy(proxy)
    ]


def find_duplicate_credentials(users: Iterable[User]) -> list[CredentialDuplicate]:
    grouped: dict[CredentialKey, list[str]] = defaultdict(list)
    for user in users:
        if user.status not in RUNNABLE_STATUSES:
            continue
        for key in credential_keys_for_user(user):
            grouped[key].append(user.username)
    return [
        CredentialDuplicate(key=key, users=tuple(sorted(usernames)))
        for key, usernames in sorted(grouped.items())
        if len(set(usernames)) > 1
    ]
```

- [ ] **Step 4: Run test and verify it passes**

Run: `/Users/zheng/Code/MarzbanX/.venv/bin/python -m pytest tests/test_credential_isolation.py::test_credential_keys_include_effective_inbound_and_protocol_credentials -q`

Expected: PASS.

- [ ] **Step 5: Add duplicate detection tests**

Append:

```python
def test_duplicate_detection_is_runnable_and_inbound_scoped(monkeypatch):
    monkeypatch.setattr(xray, "config", _config())
    alice = _user(
        1,
        "alice",
        proxies=[_proxy(ProxyTypes.Hysteria, {"auth": "shared"}, excluded=())],
    )
    bob = _user(
        2,
        "bob",
        proxies=[_proxy(ProxyTypes.Hysteria, {"auth": "shared"}, excluded=())],
    )
    charlie = _user(
        3,
        "charlie",
        status=UserStatus.disabled,
        proxies=[_proxy(ProxyTypes.Hysteria, {"auth": "shared"}, excluded=())],
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
    alice = _user(1, "alice", proxies=[_proxy(ProxyTypes.Shadowsocks, {"method": "chacha20-ietf-poly1305", "password": "same"}, excluded=("SS-B",))])
    bob = _user(2, "bob", proxies=[_proxy(ProxyTypes.Shadowsocks, {"method": "chacha20-ietf-poly1305", "password": "same"}, excluded=("SS-A",))])

    assert find_duplicate_credentials([alice, bob]) == []
```

- [ ] **Step 6: Run duplicate tests and verify they pass**

Run: `/Users/zheng/Code/MarzbanX/.venv/bin/python -m pytest tests/test_credential_isolation.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add app/xray/credential_isolation.py tests/test_credential_isolation.py
git commit -m "feat: add proxy credential duplicate audit"
```

## Task 2: Repair Helpers And CLI Audit Surface

**Files:**
- Modify: `app/xray/credential_isolation.py`
- Modify: `tests/test_credential_isolation.py`
- Modify: `cli/user.py`

- [ ] **Step 1: Write failing repair test**

Append to `tests/test_credential_isolation.py`:

```python
from app.xray.credential_isolation import repair_duplicate_credentials


def test_repair_duplicate_credentials_rotates_all_but_one_user(monkeypatch):
    monkeypatch.setattr(xray, "config", _config())
    alice = _user(1, "alice", proxies=[_proxy(ProxyTypes.AnyTLS, {"password": "same"})])
    bob = _user(2, "bob", proxies=[_proxy(ProxyTypes.AnyTLS, {"password": "same"})])

    repaired = repair_duplicate_credentials([alice, bob])

    assert repaired == ["bob"]
    assert alice.proxies[0].settings["password"] == "same"
    assert bob.proxies[0].settings["password"] != "same"
    assert find_duplicate_credentials([alice, bob]) == []
```

- [ ] **Step 2: Run repair test and verify it fails**

Run: `/Users/zheng/Code/MarzbanX/.venv/bin/python -m pytest tests/test_credential_isolation.py::test_repair_duplicate_credentials_rotates_all_but_one_user -q`

Expected: FAIL with `ImportError` or missing function.

- [ ] **Step 3: Implement repair helper**

Add to `app/xray/credential_isolation.py`:

```python
from app.models.proxy import ProxySettings


def _settings_model(proxy: Proxy):
    return ProxySettings.from_dict(ProxyTypes(proxy.type), proxy.settings or {})


def _write_settings(proxy: Proxy, settings_model):
    proxy.settings = settings_model.dict(no_obj=True)


def _rotate_proxy_credential(proxy: Proxy):
    settings_model = _settings_model(proxy)
    settings_model.revoke()
    _write_settings(proxy, settings_model)


def repair_duplicate_credentials(users: list[User]) -> list[str]:
    repaired_usernames: list[str] = []
    while True:
        duplicates = find_duplicate_credentials(users)
        if not duplicates:
            return repaired_usernames
        duplicate = duplicates[0]
        keeper = duplicate.users[0]
        for user in users:
            if user.username == keeper or user.username not in duplicate.users:
                continue
            for proxy in user.proxies:
                if duplicate.key in credential_keys_for_proxy(proxy):
                    _rotate_proxy_credential(proxy)
                    repaired_usernames.append(user.username)
                    break
```

- [ ] **Step 4: Run repair test and verify it passes**

Run: `/Users/zheng/Code/MarzbanX/.venv/bin/python -m pytest tests/test_credential_isolation.py::test_repair_duplicate_credentials_rotates_all_but_one_user -q`

Expected: PASS.

- [ ] **Step 5: Add CLI commands**

Modify `cli/user.py` imports:

```python
from app.xray.credential_isolation import credential_fingerprint, find_duplicate_credentials, repair_duplicate_credentials
```

Append commands:

```python
@app.command(name="audit-credentials")
def audit_credentials():
    """Lists duplicate runnable proxy credentials by protocol and inbound."""
    with GetDB() as db:
        users = crud.get_users(db=db)
        duplicates = find_duplicate_credentials(users)
        if not duplicates:
            utils.success("No duplicate runnable proxy credentials found.")
        utils.print_table(
            table=Table("Protocol", "Inbound", "Credential Fingerprint", "Users"),
            rows=[
                (
                    duplicate.key.protocol,
                    duplicate.key.inbound_tag,
                    credential_fingerprint(duplicate.key.credential),
                    ", ".join(duplicate.users),
                )
                for duplicate in duplicates
            ],
        )


@app.command(name="repair-credentials")
def repair_credentials(yes_to_all: bool = typer.Option(False, *utils.FLAGS["yes_to_all"], help="Skips confirmations")):
    """Rotates duplicate runnable proxy credentials. Repaired users must re-pull subscriptions."""
    with GetDB() as db:
        users = crud.get_users(db=db)
        duplicates = find_duplicate_credentials(users)
        if not duplicates:
            utils.success("No duplicate runnable proxy credentials found.")
        if not yes_to_all and not typer.confirm("Rotate duplicate credentials for all but one user in each group?"):
            utils.error("Aborted.")
        repaired = repair_duplicate_credentials(users)
        db.commit()
        utils.success("Rotated credentials for: " + ", ".join(sorted(set(repaired))))
```

- [ ] **Step 6: Run focused tests**

Run: `/Users/zheng/Code/MarzbanX/.venv/bin/python -m pytest tests/test_credential_isolation.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add app/xray/credential_isolation.py tests/test_credential_isolation.py cli/user.py
git commit -m "feat: add credential audit repair helpers"
```

## Task 3: Shadowsocks Sing-box Gate

**Files:**
- Read: `/private/tmp/sing-box-api-check`
- Read/possible modify: `/Users/zheng/Code/MarzbanX-node/src/xray_config.rs`

- [ ] **Step 1: Inspect sing-box Shadowsocks implementation**

Run:

```bash
rg -n "type Shadowsocks|Shadowsocks.*Options|MultiUser|Users|Password|ServerKey|2022" /private/tmp/sing-box-api-check -S
```

Expected: identify whether top-level `password` is an accepted fallback credential when `users` exists, or whether it is a required 2022 server key that cannot authenticate as a user.

- [ ] **Step 2: Record decision**

If source shows top-level `password` is only a 2022 server key when `users` exists, no MarzbanX-node patch is required. Commit no code for this task.

If source shows top-level `password` can authenticate clients when per-user `users` exists, request approval to edit `/Users/zheng/Code/MarzbanX-node`, then write a failing Rust test in `src/xray_config.rs` asserting user-managed Shadowsocks inbounds do not expose fallback password, implement the translator fix, and commit in the MarzbanX-node repository.

- [ ] **Step 3: Do not proceed to Task 4 until the decision is recorded**

Expected: a short note in the session and either no node diff or a committed node fix.

## Task 4: Write-Time Enforcement

**Files:**
- Modify: `app/xray/credential_isolation.py`
- Modify: `app/db/crud.py`
- Modify: `tests/test_credential_isolation.py`

- [ ] **Step 1: Write failing create/update/status enforcement tests**

Append tests that use an in-memory DB session or DBUser objects plus a validation helper:

```python
from app.xray.credential_isolation import CredentialConflictError, validate_unique_credentials


def test_validate_unique_credentials_rejects_duplicate_runnable_user(monkeypatch):
    monkeypatch.setattr(xray, "config", _config())
    existing = _user(1, "alice", proxies=[_proxy(ProxyTypes.Trojan, {"password": "same"})])
    pending = _user(2, "bob", proxies=[_proxy(ProxyTypes.Trojan, {"password": "same"})])

    with pytest.raises(CredentialConflictError) as exc_info:
        validate_unique_credentials(pending, [existing, pending])

    assert "trojan" in str(exc_info.value)
    assert "Trojan" in str(exc_info.value)


def test_validate_unique_credentials_allows_same_user_unchanged(monkeypatch):
    monkeypatch.setattr(xray, "config", _config())
    user = _user(1, "alice", proxies=[_proxy(ProxyTypes.VLESS, {"id": "33333333-3333-3333-3333-333333333333"})])

    validate_unique_credentials(user, [user])
```

- [ ] **Step 2: Run enforcement tests and verify they fail**

Run: `/Users/zheng/Code/MarzbanX/.venv/bin/python -m pytest tests/test_credential_isolation.py::test_validate_unique_credentials_rejects_duplicate_runnable_user tests/test_credential_isolation.py::test_validate_unique_credentials_allows_same_user_unchanged -q`

Expected: FAIL because validation classes/functions do not exist.

- [ ] **Step 3: Implement validation helper**

Add:

```python
class CredentialConflictError(ValueError):
    pass


def validate_unique_credentials(pending_user: User, users: Iterable[User]) -> None:
    pending_keys = set(credential_keys_for_user(pending_user))
    for user in users:
        if user.id == pending_user.id:
            continue
        if user.status not in RUNNABLE_STATUSES:
            continue
        overlap = pending_keys & set(credential_keys_for_user(user))
        if overlap:
            key = sorted(overlap)[0]
            raise CredentialConflictError(
                f"Duplicate {key.protocol} credential on inbound {key.inbound_tag} conflicts with user {user.username}"
            )
```

- [ ] **Step 4: Wire CRUD**

In `crud.create_user`, after constructing `dbuser` and before `db.add(dbuser)`, call validation with `get_users(db) + [dbuser]` when `dbuser.status` is runnable.

In `crud.update_user`, after applying the post-update state and before `db.commit()`, call validation when `dbuser.status` is runnable.

In `crud.revoke_user_sub`, after rotating credentials but before commit, call update path validation through `update_user`.

In `crud.update_user_status`, when target status is `active` or `on_hold`, set the status in memory, validate, then commit.

- [ ] **Step 5: Run focused tests**

Run: `/Users/zheng/Code/MarzbanX/.venv/bin/python -m pytest tests/test_credential_isolation.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add app/xray/credential_isolation.py app/db/crud.py tests/test_credential_isolation.py
git commit -m "feat: enforce unique proxy credentials"
```

## Task 5: Bulk Delete Runtime Synchronization

**Files:**
- Modify: `app/xray/credential_isolation.py`
- Modify: `app/routers/user.py`
- Modify: `app/jobs/remove_expired_users.py`
- Modify: `app/db/crud.py`
- Modify: `tests/test_credential_isolation.py`

- [ ] **Step 1: Write failing deletion-plan test**

Append:

```python
from app.xray.credential_isolation import build_user_removal_plan


def test_build_user_removal_plan_snapshots_runtime_fields_before_delete(monkeypatch):
    monkeypatch.setattr(xray, "config", _config())
    alice = _user(1, "alice", proxies=[_proxy(ProxyTypes.Hysteria, {"auth": "gone"})])

    plan = build_user_removal_plan([alice])

    assert plan.users[0].id == 1
    assert plan.users[0].username == "alice"
    assert plan.users[0].email == "1.alice"
    assert plan.users[0].config_reload_inbounds == frozenset({"HY2"})
```

- [ ] **Step 2: Run deletion-plan test and verify it fails**

Run: `/Users/zheng/Code/MarzbanX/.venv/bin/python -m pytest tests/test_credential_isolation.py::test_build_user_removal_plan_snapshots_runtime_fields_before_delete -q`

Expected: FAIL because function does not exist.

- [ ] **Step 3: Implement deletion plan**

Add dataclasses and helper:

```python
from app.xray.config import CONFIG_RELOAD_PROXY_TYPES


@dataclass(frozen=True)
class UserRemovalSnapshot:
    id: int
    username: str
    email: str
    config_reload_inbounds: frozenset[str]


@dataclass(frozen=True)
class UserRemovalPlan:
    users: tuple[UserRemovalSnapshot, ...]


def build_user_removal_plan(users: Iterable[User]) -> UserRemovalPlan:
    snapshots = []
    for user in users:
        reload_tags: set[str] = set()
        for proxy in user.proxies:
            if ProxyTypes(proxy.type) in CONFIG_RELOAD_PROXY_TYPES:
                reload_tags.update(effective_inbound_tags_for_proxy(proxy))
        snapshots.append(
            UserRemovalSnapshot(
                id=user.id,
                username=user.username,
                email=f"{user.id}.{user.username}",
                config_reload_inbounds=frozenset(reload_tags),
            )
        )
    return UserRemovalPlan(users=tuple(snapshots))
```

- [ ] **Step 4: Add runtime cleanup helper**

Add to `app/xray/operations.py`:

```python
def remove_users_from_runtime(removal_plan):
    reload_inbounds = set()
    for user in removal_plan.users:
        reload_inbounds.update(user.config_reload_inbounds)
        for inbound_tag in xray.config.inbounds_by_tag:
            if not _inbound_uses_xray_api(inbound_tag):
                continue
            _remove_user_from_inbound(xray.api, inbound_tag, user.email)
            for node_api in _node_apis_for_inbound(xray.nodes.values(), inbound_tag):
                _remove_user_from_inbound(node_api, inbound_tag, user.email)

    if reload_inbounds:
        _restart_started_nodes_for_config_reload(reload_inbounds)
```

Add `remove_users_from_runtime` to `__all__`.

- [ ] **Step 5: Split autodelete selection from deletion**

In `app/db/crud.py`, extract the query body currently inside `autodelete_expired_users`:

```python
def get_autodeletable_expired_users(
        db: Session,
        include_limited_users: bool = False) -> List[User]:
    target_status = (
        [UserStatus.expired] if not include_limited_users
        else [UserStatus.expired, UserStatus.limited]
    )

    auto_delete = coalesce(User.auto_delete_in_days, USERS_AUTODELETE_DAYS)
    query = db.query(User, auto_delete).filter(
        auto_delete >= 0,
        User.status.in_(target_status),
    ).options(joinedload(User.admin))

    return [
        user
        for (user, auto_delete) in query
        if user.last_status_change + timedelta(days=auto_delete) <= datetime.utcnow()
    ]


def autodelete_expired_users(db: Session,
                             include_limited_users: bool = False) -> List[User]:
    expired_users = get_autodeletable_expired_users(db, include_limited_users)
    if expired_users:
        remove_users(db, expired_users)
    return expired_users
```

- [ ] **Step 6: Wire API bulk deletion**

In `app/routers/user.py`, import `build_user_removal_plan`. In `delete_expired_users`, before `crud.remove_users(db, expired_users)`, add:

```python
    removal_plan = build_user_removal_plan(expired_users)
```

After `crud.remove_users(db, expired_users)`, add:

```python
    bg.add_task(xray.operations.remove_users_from_runtime, removal_plan=removal_plan)
```

- [ ] **Step 7: Wire scheduled autodelete**

In `app/jobs/remove_expired_users.py`, replace the direct `crud.autodelete_expired_users` call with:

```python
        expired_users = crud.get_autodeletable_expired_users(db, USER_AUTODELETE_INCLUDE_LIMITED_ACCOUNTS)
        removal_plan = build_user_removal_plan(expired_users)
        if expired_users:
            crud.remove_users(db, expired_users)
            xray.operations.remove_users_from_runtime(removal_plan)
        deleted_users = expired_users
```

Add imports:

```python
from app import xray
from app.xray.credential_isolation import build_user_removal_plan
```

- [ ] **Step 8: Run focused tests**

Run: `/Users/zheng/Code/MarzbanX/.venv/bin/python -m pytest tests/test_credential_isolation.py tests/test_hysteria_support.py tests/test_node_active_inbounds.py -q`

Expected: PASS.

- [ ] **Step 9: Commit**

Run:

```bash
git add app/xray/credential_isolation.py app/xray/operations.py app/routers/user.py app/jobs/remove_expired_users.py app/db/crud.py tests/test_credential_isolation.py
git commit -m "fix: sync runtime on bulk user deletion"
```

## Task 6: Documentation And Final Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-06-proxy-credential-isolation-design.md` only if implementation reality differs from the spec.

- [ ] **Step 1: Update docs**

Add a short section to `README.md` near user/protocol support:

```markdown
### Proxy Credential Isolation

MarzbanX treats per-user proxy credentials as the node-access security boundary. VMess/VLESS UUIDs and Trojan, Shadowsocks, HY2, and AnyTLS passwords must be unique for every runnable user on the same inbound. Deleting a user removes that user's runtime credential; remaining users do not need to re-pull subscriptions.

Use `marzban-cli user audit-credentials` to list duplicate runnable credentials and `marzban-cli user repair-credentials --yes` to rotate duplicates. Users whose credentials are repaired must re-pull subscriptions.
```

- [ ] **Step 2: Run verification suite**

Run:

```bash
/Users/zheng/Code/MarzbanX/.venv/bin/python -m pytest tests/test_credential_isolation.py tests/test_hysteria_support.py tests/test_node_active_inbounds.py tests/test_record_usages.py -q
```

Expected: PASS.

- [ ] **Step 3: Commit**

Run:

```bash
git add README.md docs/superpowers/specs/2026-07-06-proxy-credential-isolation-design.md
git commit -m "docs: document proxy credential isolation"
```

- [ ] **Step 4: Final subagent review**

Request subagent review over `48c5ddce1dae757a87420ae0e93c4496df481092..HEAD` with requirements from this plan and the design spec. Fix any Critical or Important findings before completion.
