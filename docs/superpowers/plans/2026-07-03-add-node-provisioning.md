# Add Node Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a panel Add Node provisioning flow that creates generated inbounds, hosts, a panel-managed node, and a one-command installer.

**Architecture:** Add a focused controller-side provisioning module that owns generated inbound templates, config mutation, node creation, install token lifecycle, and installer payload generation. Keep Rust node behavior unchanged unless regression tests reveal a gap, because the current Rust node already supports controller-managed inbound selection.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy/Alembic, pytest, React/Chakra/react-query, shell installer script.

---

## File Structure

- Create `app/models/node_provision.py`: request/response models for provisioning, protocol enum, install redeem payload.
- Create `app/xray/node_provisioning.py`: generated inbound templates, core selection, config mutation helper, token generation/redeem service.
- Modify `app/db/models.py`: add `NodeProvisionToken` ORM table.
- Modify `app/db/crud.py`: add token CRUD helpers and optional generated host helper.
- Create `app/db/migrations/versions/9f3a7c2d8b11_node_provision_tokens.py`: token table migration.
- Modify `app/routers/node.py`: add `/node/provision`, `/node/provision/redeem`, and `/node/install.sh`.
- Create `tests/test_node_provisioning.py`: backend provisioning and token tests.
- Modify `app/dashboard/src/contexts/NodesContext.tsx`: add provisioning API model/action.
- Modify `app/dashboard/src/components/NodesModal.tsx`: replace default Add Node form with provisioning wizard and keep manual form as advanced.
- Modify locale files under `app/dashboard/public/statics/locales/` and rebuild dashboard output.
- Modify `Marzban-node/README.md` or `DEPLOYMENT.md` only if installer behavior needs node-side documentation.

## Task 1: Backend Models And Red Tests

**Files:**
- Create: `app/models/node_provision.py`
- Create: `tests/test_node_provisioning.py`

- [ ] **Step 1: Write failing tests for protocol policy and token hashing**

Add tests that import the planned service functions before they exist:

```python
from datetime import datetime, timedelta, timezone

import pytest

from app.models.node_provision import NodeProvisionProtocol
from app.xray.node_provisioning import (
    choose_core_kind,
    hash_install_token,
    verify_install_token,
)


def test_choose_core_kind_uses_sing_box_when_hy2_is_selected():
    assert choose_core_kind([NodeProvisionProtocol.hy2]) == "sing-box"
    assert choose_core_kind([
        NodeProvisionProtocol.hy2,
        NodeProvisionProtocol.vless_reality,
        NodeProvisionProtocol.shadowsocks,
    ]) == "sing-box"


def test_choose_core_kind_uses_xray_for_xray_only_protocols():
    assert choose_core_kind([NodeProvisionProtocol.vless_reality]) == "xray"
    assert choose_core_kind([NodeProvisionProtocol.shadowsocks]) == "xray"


def test_install_token_hash_does_not_store_plaintext():
    token = "plain-token"
    token_hash = hash_install_token(token)

    assert token_hash != token
    assert verify_install_token(token, token_hash)
    assert not verify_install_token("other-token", token_hash)
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
pytest tests/test_node_provisioning.py -q
```

Expected: FAIL because `app.models.node_provision` or `app.xray.node_provisioning` does not exist.

- [ ] **Step 3: Implement minimal models and helpers**

Create `app/models/node_provision.py` with:

```python
from enum import Enum
from typing import List

from pydantic import BaseModel, Field


class NodeProvisionProtocol(str, Enum):
    hy2 = "hy2"
    vless_reality = "vless-reality"
    shadowsocks = "shadowsocks"


class NodeProvisionInbound(BaseModel):
    protocol: NodeProvisionProtocol
    port: int = Field(ge=1, le=65535)


class NodeProvisionCreate(BaseModel):
    name: str
    address: str
    port: int = Field(default=62050, ge=1, le=65535)
    api_port: int = Field(default=62051, ge=1, le=65535)
    usage_coefficient: float = Field(default=1.0, gt=0)
    inbounds: List[NodeProvisionInbound]
```

Create `app/xray/node_provisioning.py` with:

```python
import hashlib
import hmac

from app.models.node_provision import NodeProvisionProtocol


def choose_core_kind(protocols):
    return "sing-box" if NodeProvisionProtocol.hy2 in set(protocols) else "xray"


def hash_install_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_install_token(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_install_token(token), token_hash)
```

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
pytest tests/test_node_provisioning.py -q
```

Expected: PASS for the initial tests.

## Task 2: Generated Inbounds And Config Mutation

**Files:**
- Modify: `app/xray/node_provisioning.py`
- Modify: `tests/test_node_provisioning.py`

- [ ] **Step 1: Write failing tests for generated inbound templates**

Add:

```python
from app.xray import XRayConfig
from app.xray.node_provisioning import build_generated_inbounds


def test_build_generated_inbounds_creates_hy2_vless_and_shadowsocks_templates():
    inbounds = build_generated_inbounds(
        node_id=42,
        specs=[
            (NodeProvisionProtocol.hy2, 8443),
            (NodeProvisionProtocol.vless_reality, 443),
            (NodeProvisionProtocol.shadowsocks, 8388),
        ],
    )

    assert [item["tag"] for item in inbounds] == [
        "node-42-hy2-8443",
        "node-42-vless-443",
        "node-42-shadowsocks-8388",
    ]
    assert inbounds[0]["protocol"] == "hysteria"
    assert inbounds[0]["settings"]["version"] == 2
    assert inbounds[0]["streamSettings"]["network"] == "hysteria"
    assert inbounds[1]["protocol"] == "vless"
    assert inbounds[2]["protocol"] == "shadowsocks"


def test_generated_inbounds_are_visible_to_xray_config():
    config = {
        "inbounds": build_generated_inbounds(
            node_id=42,
            specs=[(NodeProvisionProtocol.hy2, 8443)],
        ),
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
    }

    parsed = XRayConfig(config)
    assert parsed.inbounds_by_tag["node-42-hy2-8443"]["protocol"] == "hysteria"
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
pytest tests/test_node_provisioning.py -q
```

Expected: FAIL because `build_generated_inbounds` does not exist.

- [ ] **Step 3: Implement generated inbound builders**

Add functions for deterministic tags and minimal templates. Use Xray-compatible JSON structures accepted by existing `XRayConfig`.

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
pytest tests/test_node_provisioning.py -q
```

Expected: PASS.

## Task 3: Token Persistence And Redeem

**Files:**
- Modify: `app/db/models.py`
- Modify: `app/db/crud.py`
- Create: `app/db/migrations/versions/9f3a7c2d8b11_node_provision_tokens.py`
- Modify: `app/xray/node_provisioning.py`
- Modify: `tests/test_node_provisioning.py`

- [ ] **Step 1: Write failing tests for token persistence**

Add tests that create a token row, assert plaintext is not stored, redeem once, and reject second redeem.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
pytest tests/test_node_provisioning.py -q
```

Expected: FAIL because ORM model and CRUD helpers do not exist.

- [ ] **Step 3: Implement `NodeProvisionToken` model and CRUD**

Add a SQLAlchemy model with `node_id`, `token_hash`, `created_by`, `created_at`, `expires_at`, `redeemed_at`, `revoked_at`, `active_inbounds_json`, and `core_kind`. Add CRUD helpers to create, find by hash, redeem, and revoke.

- [ ] **Step 4: Add Alembic migration**

Create the table with indexes for `node_id` and `token_hash`.

- [ ] **Step 5: Run tests and verify GREEN**

Run:

```bash
pytest tests/test_node_provisioning.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit backend foundation**

Run:

```bash
git add app/models/node_provision.py app/xray/node_provisioning.py app/db/models.py app/db/crud.py app/db/migrations/versions/9f3a7c2d8b11_node_provision_tokens.py tests/test_node_provisioning.py
git commit -m "feat(core): add node provisioning foundation"
```

## Task 4: Provisioning API

**Files:**
- Modify: `app/routers/node.py`
- Modify: `app/xray/node_provisioning.py`
- Modify: `tests/test_node_provisioning.py`

- [ ] **Step 1: Write failing route/service tests**

Tests must prove that provisioning creates generated inbounds in config, creates hosts only for generated tags, creates a panel-mode node, and returns an install command containing `/api/node/install.sh`.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
pytest tests/test_node_provisioning.py -q
```

Expected: FAIL because `/node/provision` route/service does not exist.

- [ ] **Step 3: Implement provisioning service**

Build candidate config in memory, validate with `XRayConfig`, persist through an atomic config helper, create DB inbounds/hosts/node/token, and schedule existing connect flow.

- [ ] **Step 4: Implement router endpoint**

Add `POST /api/node/provision` using `Admin.check_sudo_admin`.

- [ ] **Step 5: Run tests and verify GREEN**

Run:

```bash
pytest tests/test_node_provisioning.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit API**

Run:

```bash
git add app/routers/node.py app/xray/node_provisioning.py tests/test_node_provisioning.py
git commit -m "feat(core): provision nodes from generated inbounds"
```

## Task 5: Installer Script And Redeem API

**Files:**
- Modify: `app/routers/node.py`
- Modify: `app/models/node_provision.py`
- Modify: `app/xray/node_provisioning.py`
- Modify: `tests/test_node_provisioning.py`

- [ ] **Step 1: Write failing tests**

Tests must prove `/api/node/install.sh` returns a fixed shell script, redeem rejects missing/invalid/expired/reused tokens, redeem returns no controller private key, and payload omits `INBOUNDS`.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
pytest tests/test_node_provisioning.py -q
```

Expected: FAIL because installer endpoints do not exist.

- [ ] **Step 3: Implement fixed installer script response**

Script parses `--token`, calls redeem endpoint, writes env without `INBOUNDS`, installs only the required core according to `core_kind`, writes service file, and restarts systemd service.

- [ ] **Step 4: Implement redeem endpoint**

Add `POST /api/node/provision/redeem`. Mark token redeemed before returning payload to enforce one-time use.

- [ ] **Step 5: Run tests and verify GREEN**

Run:

```bash
pytest tests/test_node_provisioning.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit installer API**

Run:

```bash
git add app/routers/node.py app/models/node_provision.py app/xray/node_provisioning.py tests/test_node_provisioning.py
git commit -m "feat(core): serve node provisioning installer"
```

## Task 6: Dashboard Wizard

**Files:**
- Modify: `app/dashboard/src/contexts/NodesContext.tsx`
- Modify: `app/dashboard/src/components/NodesModal.tsx`
- Modify: `app/dashboard/public/statics/locales/en.json`
- Modify: `app/dashboard/public/statics/locales/zh.json`
- Modify: dashboard build output under `app/dashboard/build/`

- [ ] **Step 1: Add TypeScript API shape**

Add `provisionNode()` to `NodesContext` that posts to `/node/provision`.

- [ ] **Step 2: Replace default Add Node flow**

Update `AddNodeForm` to collect identity, protocol checkboxes, per-protocol ports, expected core preview, and show install command after success. Keep the old manual form behind an advanced toggle.

- [ ] **Step 3: Build dashboard**

Run:

```bash
cd app/dashboard
npm run build
```

Expected: TypeScript and Vite build succeed.

- [ ] **Step 4: Commit dashboard**

Run:

```bash
git add app/dashboard/src app/dashboard/public/statics/locales app/dashboard/build
git commit -m "feat(dashboard): add node provisioning wizard"
```

## Task 7: Verification And Review

**Files:**
- Review all changed files.

- [ ] **Step 1: Run backend tests**

Run:

```bash
pytest tests/test_node_provisioning.py tests/test_node_active_inbounds.py tests/test_hysteria_support.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run dashboard build**

Run:

```bash
cd app/dashboard
npm run build
```

Expected: build passes.

- [ ] **Step 3: Request subagent code review**

Ask a reviewer to inspect the diff from `aa06c77` to `HEAD` for Critical/Important issues against the design spec.

- [ ] **Step 4: Fix Critical/Important review findings**

Do not complete the goal until the final review reports no Critical or Important issues.

- [ ] **Step 5: Final completion audit**

Verify every requirement in the original objective:

- panel Add Node path exists;
- name/IP/protocol/port can be entered;
- controller creates inbound;
- controller creates host;
- controller creates node;
- controller generates install command;
- installer endpoint exists;
- installer avoids manual `INBOUNDS`;
- implementation is split into commits;
- subagent review has no high-risk findings.
