# Node-Owned Inbounds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace MarzbanX's node-facing global inbound reuse with node-owned inbounds: every panel-managed inbound belongs to exactly one node, nodes run only their own inbounds, and users remain managed by protocol.

**Architecture:** `ProxyInbound.owner_node_id` becomes the authoritative ownership field for panel-managed runtime selection. `node_inbounds_association` remains as enabled/active state, but panel-mode active inbounds must always be a subset of rows owned by that node. `/api/inbounds` remains config-derived for protocol/network/port data and is enriched with DB ownership metadata for UI and validation.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, Pydantic, pytest, React, Chakra UI, Zustand, react-query, zod.

---

## Reviewed Risk Decisions

Subagent review found the initial direction is best practice for this codebase, but implementation must address these risks before coding:

1. `owner_node_id` is the single source of truth for ownership. `node_inbounds_association` is only activation state.
2. Panel mode invariant: `active_inbounds ⊆ owned_inbounds(node.id)`.
3. Manual Add Node cannot select active inbounds because the node has no id yet. Manual advanced add stays legacy-only until a separate owned-inbound creation flow exists.
4. `/api/inbounds` must annotate `xray.config.inbounds_by_protocol` entries from DB rows; changing only the Pydantic model is insufficient.
5. Deleting a node removes owned generated inbounds by `owner_node_id`, not only tags currently active in `node_inbounds_association`.
6. Migration backfill from tag is conservative: only tags matching `^node-(\d+)-` with an existing node are assigned. Conflicts are not guessed.
7. User management remains protocol-based and must not be filtered by node ownership.
8. Rust MarzbanX-node needs no protocol change; ownership is controller/database/UI policy.

## File Structure

- `app/db/models.py`: add `ProxyInbound.owner_node_id`, `ProxyInbound.owner_node`, and `Node.owned_inbounds`.
- `app/db/migrations/versions/3b8c1d2e4f6a_node_owned_inbounds.py`: add nullable owner column, FK, index, and conservative generated-tag backfill.
- `app/models/proxy.py`: expose `owner_node_id` in inbound API responses.
- `app/routers/system.py`: return config-derived inbound entries enriched with DB ownership metadata.
- `app/db/crud.py`: add inbound ownership helper used by API, route validation, and runtime validation.
- `app/routers/node.py`: validate ownership with DB context and reject cross-owned/unowned inbounds for panel mode.
- `app/xray/node_provisioning.py`: set owner during provisioning and delete owned generated inbounds by owner id.
- `app/dashboard/src/contexts/DashboardContext.tsx`: add `owner_node_id` to `InboundType`.
- `app/dashboard/src/components/NodesModal.tsx`: show node-owned inbound choices only; surface invalid selected tags instead of silently hiding them.
- `app/dashboard/public/statics/locales/{en,zh,fa,ru}.json`: update node inbound copy.
- `tests/test_node_active_inbounds.py`: ownership validation tests.
- `tests/test_node_provisioning.py`: provisioning owner and deletion tests.
- `tests/test_system_inbounds.py`: `/api/inbounds` ownership serialization tests.
- `README.md`, `README-zh-cn.md`, `docs/node-provisioning.md`: document node-owned inbound model.

## Requirements

1. Panel-managed nodes can only enable inbounds whose `owner_node_id` equals their `node.id`.
2. Panel-managed nodes reject unowned inbounds. Unowned legacy inbounds remain in DB/config for migration and user exclusion/template references, but are not selectable/runtime-valid for panel nodes.
3. Add Node provisioning assigns ownership to every generated inbound.
4. Existing generated inbounds named `node-{id}-{protocol}-{port}` are backfilled to `owner_node_id=id` when the node exists.
5. Manual advanced Add Node remains legacy-only and cannot create a panel-managed node with active inbound selections.
6. Users continue to be managed by protocol. User creation/update still applies to all matching protocol inbounds in `xray.config.inbounds_by_protocol`.
7. Subscriptions continue to be generated from proxy hosts attached to inbounds; node-owned inbounds produce per-node subscription entries.
8. Existing invalid active selections are visible in the edit UI as invalid/migration-needed selections and rejected on save until removed.
9. Work is committed in batches, with tests run before each commit.
10. Runtime connect/restart paths enforce the same ownership invariant as the API. Existing invalid panel selections fail with a clear node error instead of continuing to run.
11. SQLite deployments can run the migration; FK/index changes use Alembic batch operations where needed.

---

### Task 1: Backend Ownership Model And Migration

**Files:**
- Modify: `app/db/models.py`
- Create: `app/db/migrations/versions/3b8c1d2e4f6a_node_owned_inbounds.py`
- Modify: `app/xray/node_provisioning.py`
- Test: `tests/test_node_provisioning.py`

- [ ] **Step 1: Write failing provisioning owner test**

Add this test to `tests/test_node_provisioning.py` near existing provisioning creation tests:

```python
def test_provision_node_sets_generated_inbound_owner_id(monkeypatch):
    import app.xray.node_provisioning as node_provisioning

    db = _db_session()
    monkeypatch.setattr(node_provisioning, "generate_reality_key_pair", lambda: ("priv", "pub"))
    payload = NodeProvisionCreate(
        name="owned-node",
        address="203.0.113.10",
        inbounds=[NodeProvisionInbound(protocol=NodeProvisionProtocol.vless_reality, port=443)],
    )
    result = provision_node(
        db,
        payload,
        admin_username="admin",
        controller_url="https://panel.example.com",
        current_config={"log": {"loglevel": "warning"}, "inbounds": [], "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}]},
        apply_config=lambda config: None,
        binary_url="https://panel.example.com/download/marzban-node",
        xray_install_url="https://panel.example.com/download/install-xray.sh",
        sing_box_install_url="https://panel.example.com/download/install-sing-box.sh",
    )

    inbound = db.query(ProxyInbound).filter_by(tag=result.active_inbounds[0]).one()
    assert inbound.owner_node_id == result.node.id
```

If this file imports `app.xray.node_provisioning` under a different alias, use that existing alias.

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python -m pytest tests/test_node_provisioning.py::test_provision_node_sets_generated_inbound_owner_id -q
```

Expected: FAIL with missing `owner_node_id`.

- [ ] **Step 3: Add SQLAlchemy ownership fields**

In `app/db/models.py`, add to `ProxyInbound`:

```python
owner_node_id = Column(Integer, ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True, index=True)
owner_node = relationship("Node", back_populates="owned_inbounds", foreign_keys=[owner_node_id])
```

In `Node`, add:

```python
owned_inbounds = relationship(
    "ProxyInbound",
    back_populates="owner_node",
    foreign_keys="ProxyInbound.owner_node_id",
)
```

- [ ] **Step 4: Add Alembic migration**

Create `app/db/migrations/versions/3b8c1d2e4f6a_node_owned_inbounds.py`:

```python
"""node owned inbounds

Revision ID: 3b8c1d2e4f6a
Revises: 9f3a7c2d8b11
Create Date: 2026-07-06 00:00:00.000000

"""
from alembic import op
import re
import sqlalchemy as sa


revision = "3b8c1d2e4f6a"
down_revision = "9f3a7c2d8b11"
branch_labels = None
depends_on = None

_GENERATED_TAG = re.compile(r"^node-(\d+)-(hy2|anytls|vless|vmess|trojan|shadowsocks|ss)-(\d+)$")


def upgrade() -> None:
    with op.batch_alter_table("inbounds") as batch_op:
        batch_op.add_column(sa.Column("owner_node_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_inbounds_owner_node_id", ["owner_node_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_inbounds_owner_node_id_nodes",
            "nodes",
            ["owner_node_id"],
            ["id"],
            ondelete="SET NULL",
        )

    bind = op.get_bind()
    node_ids = {row[0] for row in bind.execute(sa.text("SELECT id FROM nodes")).fetchall()}
    inbound_tags = [row[0] for row in bind.execute(sa.text("SELECT tag FROM inbounds")).fetchall()]
    for tag in inbound_tags:
        match = _GENERATED_TAG.match(tag or "")
        if not match:
            continue
        node_id = int(match.group(1))
        if node_id not in node_ids:
            continue
        bind.execute(
            sa.text("UPDATE inbounds SET owner_node_id = :node_id WHERE tag = :tag"),
            {"node_id": node_id, "tag": tag},
        )


def downgrade() -> None:
    with op.batch_alter_table("inbounds") as batch_op:
        batch_op.drop_constraint("fk_inbounds_owner_node_id_nodes", type_="foreignkey")
        batch_op.drop_index("ix_inbounds_owner_node_id")
        batch_op.drop_column("owner_node_id")
```

- [ ] **Step 5: Set owner during provisioning**

Change `app/xray/node_provisioning.py`:

```python
inbound_row = ProxyInbound(tag=inbound["tag"], owner_node_id=dbnode.id)
```

- [ ] **Step 6: Run tests and commit**

Run:

```bash
python -m pytest tests/test_node_provisioning.py -q
XRAY_EXECUTABLE_PATH=/bin/echo alembic heads
```

Expected: tests pass; Alembic reports one head.

Commit:

```bash
git add app/db/models.py app/db/migrations/versions/3b8c1d2e4f6a_node_owned_inbounds.py app/xray/node_provisioning.py tests/test_node_provisioning.py
git commit -m "feat(core): track node-owned inbounds"
```

---

### Task 2: Backend Ownership Validation

**Files:**
- Modify: `app/db/crud.py`
- Modify: `app/routers/node.py`
- Test: `tests/test_node_active_inbounds.py`

- [ ] **Step 1: Write failing validation tests**

Add tests to `tests/test_node_active_inbounds.py`:

```python
def test_node_inbounds_validation_rejects_cross_owned_inbound(monkeypatch):
    config = XRayConfig({
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": "node-2-vless-443", "protocol": "vless", "port": 443}],
        "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
    })
    monkeypatch.setattr(operations.xray, "config", config)
    monkeypatch.setattr(operations.xray, "hosts", {"node-2-vless-443": [{"address": "example.com"}]})

    with pytest.raises(Exception) as exc_info:
        validate_inbounds_selection(
            NodeInboundsMode.panel,
            ["node-2-vless-443"],
            node_id=1,
            inbound_owner_ids={"node-2-vless-443": 2},
        )

    assert getattr(exc_info.value, "status_code") == 400
    assert "another node" in exc_info.value.detail.lower()


def test_node_inbounds_validation_rejects_unowned_inbound_in_panel_mode(monkeypatch):
    config = XRayConfig({
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": "VLESS", "protocol": "vless", "port": 443}],
        "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
    })
    monkeypatch.setattr(operations.xray, "config", config)
    monkeypatch.setattr(operations.xray, "hosts", {"VLESS": [{"address": "example.com"}]})

    with pytest.raises(Exception) as exc_info:
        validate_inbounds_selection(
            NodeInboundsMode.panel,
            ["VLESS"],
            node_id=1,
            inbound_owner_ids={"VLESS": None},
        )

    assert getattr(exc_info.value, "status_code") == 400
    assert "not owned" in exc_info.value.detail.lower()
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m pytest tests/test_node_active_inbounds.py::test_node_inbounds_validation_rejects_cross_owned_inbound tests/test_node_active_inbounds.py::test_node_inbounds_validation_rejects_unowned_inbound_in_panel_mode -q
```

Expected: FAIL because validation lacks ownership parameters.

- [ ] **Step 3: Add ownership helpers**

In `app/db/crud.py`, add:

```python
def get_inbound_owner_ids(db: Session, inbound_tags: List[str]) -> Dict[str, Optional[int]]:
    if not inbound_tags:
        return {}
    rows = (
        db.query(ProxyInbound.tag, ProxyInbound.owner_node_id)
        .filter(ProxyInbound.tag.in_(inbound_tags))
        .all()
    )
    return {tag: owner_node_id for tag, owner_node_id in rows}
```

Do not add a global `get_or_create_inbound()` rejection for `node-` tags. Host and user-exclusion flows may lazily create DB rows from config-only legacy inbounds; ownership enforcement belongs in node provisioning, node activation validation, and runtime validation.

- [ ] **Step 4: Update validation signature and ownership check**

Change `validate_inbounds_selection()` in `app/routers/node.py` to accept:

```python
node_id: Optional[int] = None,
inbound_owner_ids: Optional[dict[str, Optional[int]]] = None,
```

After panel-mode check, add:

```python
if node_id is not None and inbound_owner_ids is not None:
    cross_owned = []
    unowned = []
    for tag in active_inbounds:
        owner_id = inbound_owner_ids.get(tag)
        if owner_id is None:
            unowned.append(tag)
        elif owner_id != node_id:
            cross_owned.append(tag)
    if cross_owned:
        raise HTTPException(
            status_code=400,
            detail=f"Inbound(s) belong to another node and cannot be enabled here: {', '.join(cross_owned)}",
        )
    if unowned:
        raise HTTPException(
            status_code=400,
            detail=f"Inbound(s) are not owned by this node: {', '.join(unowned)}",
        )
```

- [ ] **Step 5: Wire route validation**

In `modify_node()`, compute owner ids:

```python
owner_ids = crud.get_inbound_owner_ids(db, active_inbounds)
validate_inbounds_selection(
    inbounds_mode,
    active_inbounds,
    runtime_node=xray.nodes.get(dbnode.id),
    node_id=dbnode.id,
    inbound_owner_ids=owner_ids,
)
```

In `add_node()`, manual Add Node has no node id. Reject panel active selection explicitly before `crud.create_node()`:

```python
if new_node.inbounds_mode == NodeInboundsMode.panel or new_node.active_inbounds:
    raise HTTPException(status_code=400, detail="Manual Add Node cannot enable inbounds. Use Add Node provisioning to create node-owned inbounds.")
```

Then call `validate_inbounds_selection()` only for legacy/manual node basics if still needed.

- [ ] **Step 6: Run tests and commit**

Run:

```bash
python -m pytest tests/test_node_active_inbounds.py tests/test_node_provisioning.py -q
```

Commit:

```bash
git add app/db/crud.py app/routers/node.py tests/test_node_active_inbounds.py
git commit -m "fix(core): enforce node-owned inbound selection"
```

---

### Task 2.5: Runtime Ownership Enforcement

**Files:**
- Modify: `app/xray/operations.py`
- Modify: `app/db/crud.py`
- Test: `tests/test_node_active_inbounds.py`

- [ ] **Step 1: Write failing runtime validation test**

Add this test to `tests/test_node_active_inbounds.py`:

```python
def test_node_runtime_active_inbounds_rejects_cross_owned_panel_selection():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    with TestingSession() as db:
        node = DBNode(
            name="n1",
            address="203.0.113.1",
            port=62050,
            api_port=62051,
            inbounds_mode=NodeInboundsMode.panel,
        )
        other = DBNode(
            name="n2",
            address="203.0.113.2",
            port=62050,
            api_port=62051,
            inbounds_mode=NodeInboundsMode.panel,
        )
        db.add_all([node, other])
        db.flush()
        inbound = ProxyInbound(tag="node-2-vless-443", owner_node_id=other.id)
        node.active_inbound_objects = [inbound]
        db.add(inbound)
        db.commit()

        with pytest.raises(ValueError) as exc_info:
            operations.node_runtime_active_inbounds(db, node)

        assert "belongs to another node" in str(exc_info.value).lower()
```

Use the actual imported SQLAlchemy node model alias in this test file; if it is named `Node`, use `Node` instead of `DBNode`.

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python -m pytest tests/test_node_active_inbounds.py::test_node_runtime_active_inbounds_rejects_cross_owned_panel_selection -q
```

Expected: FAIL because runtime helper does not exist or does not validate ownership.

- [ ] **Step 3: Add runtime ownership helper**

In `app/xray/operations.py`, replace the private `_node_active_inbounds(dbnode)` behavior with a DB-aware helper:

```python
def node_runtime_active_inbounds(db: Session, dbnode: DBNode) -> list[str] | None:
    if dbnode.inbounds_mode != NodeInboundsMode.panel:
        return None

    tags = dbnode.active_inbounds
    owner_ids = crud.get_inbound_owner_ids(db, tags)
    cross_owned = [tag for tag in tags if owner_ids.get(tag) not in (None, dbnode.id)]
    unowned = [tag for tag in tags if owner_ids.get(tag) is None]

    if cross_owned:
        raise ValueError(
            f"Panel node {dbnode.id} cannot run inbound(s) that belong to another node: {', '.join(cross_owned)}"
        )
    if unowned:
        raise ValueError(
            f"Panel node {dbnode.id} cannot run unowned inbound(s): {', '.join(unowned)}"
        )
    if not tags:
        raise ValueError(f"Panel node {dbnode.id} must have at least one owned inbound")

    return tags
```

If `operations.py` does not already import `crud`, `Session`, or `NodeInboundsMode`, add imports matching local style. Keep legacy nodes returning `None` so Rust node-side `INBOUNDS` remains legacy/manual behavior only.

- [ ] **Step 4: Add operation-boundary tests for reused node objects**

Add tests proving `connect_node()` and `restart_node()` validate ownership before calling `start()` or `restart()` even when `xray.nodes[node_id]` already contains a connected runtime object. Because `threaded_function` starts a daemon thread and does not expose `__wrapped__`, call the public function and poll the DB status briefly. Use a fake `GetDB` context manager bound to the test session factory.

```python
class _TestGetDB:
    def __init__(self, Session):
        self.Session = Session
        self.db = None

    def __enter__(self):
        self.db = self.Session()
        return self.db

    def __exit__(self, exc_type, exc_value, traceback):
        self.db.close()


class _FakeRuntimeNode:
    connected = True
    started = False
    active_inbounds = None

    def start(self, config):
        raise AssertionError("start must not be called for invalid panel inbounds")

    def restart(self, config):
        raise AssertionError("restart must not be called for invalid panel inbounds")


def _wait_for_error_status(Session, node_id):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with Session() as db:
            node = get_node_by_id(db, node_id)
            if node.status == NodeStatus.error:
                return node
        time.sleep(0.02)
    raise AssertionError("node did not enter error status")


def test_connect_node_rejects_invalid_reused_panel_node_before_start(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        node = DBNode(name="n1", address="203.0.113.1", port=62050, api_port=62051, inbounds_mode=NodeInboundsMode.panel)
        db.add(node)
        db.flush()
        inbound = ProxyInbound(tag="node-1-vless-443", owner_node_id=None)
        node.active_inbound_objects = [inbound]
        db.add(inbound)
        db.commit()
        node_id = node.id

    monkeypatch.setattr(operations, "GetDB", lambda: _TestGetDB(Session))
    fake_node = _FakeRuntimeNode()
    operations.xray.nodes[node_id] = fake_node

    operations.connect_node(node_id, config=_config())

    errored = _wait_for_error_status(Session, node_id)
    assert "unowned" in errored.message.lower()


def test_restart_node_rejects_invalid_reused_panel_node_before_restart(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        node = DBNode(name="n1", address="203.0.113.1", port=62050, api_port=62051, inbounds_mode=NodeInboundsMode.panel)
        db.add(node)
        db.flush()
        inbound = ProxyInbound(tag="node-1-vless-443", owner_node_id=None)
        node.active_inbound_objects = [inbound]
        db.add(inbound)
        db.commit()
        node_id = node.id

    monkeypatch.setattr(operations, "GetDB", lambda: _TestGetDB(Session))
    fake_node = _FakeRuntimeNode()
    fake_node.connected = True
    operations.xray.nodes[node_id] = fake_node

    operations.restart_node(node_id, config=_config())

    errored = _wait_for_error_status(Session, node_id)
    assert "unowned" in errored.message.lower()
```

Add `import time`, `from sqlalchemy.pool import StaticPool`, and `NodeStatus` imports if this test file does not already have them.

- [ ] **Step 5: Use helper on every start/restart, including reused nodes**

In `connect_node()` and `restart_node()`, load `dbnode` and compute `active_inbounds = node_runtime_active_inbounds(db, dbnode)` inside the `GetDB()` session. Before every `node.start(config)` and `node.restart(config)`, including reused `xray.nodes[node_id]` objects, assign:

```python
node.active_inbounds = active_inbounds
```

When constructing a new runtime node, pass:

```python
active_inbounds=active_inbounds
```

Do not compute ownership after the DB session is closed. If the helper raises `ValueError`, mark the node errored/log the message using the existing node error path and do not call `start()` or `restart()`.

- [ ] **Step 6: Add unowned and empty helper tests**

Add helper-level tests for:

```python
def test_node_runtime_active_inbounds_rejects_unowned_panel_selection():
    # Seed a panel node with active inbound whose ProxyInbound.owner_node_id is None.
    # Assert operations.node_runtime_active_inbounds raises ValueError containing "unowned".


def test_node_runtime_active_inbounds_rejects_empty_panel_selection():
    # Seed a panel node with no active_inbound_objects.
    # Assert operations.node_runtime_active_inbounds raises ValueError containing "at least one".
```

- [ ] **Step 7: Run tests and commit**

Run:

```bash
python -m pytest tests/test_node_active_inbounds.py tests/test_node_provisioning.py -q
```

Commit:

```bash
git add app/xray/operations.py app/db/crud.py tests/test_node_active_inbounds.py
git commit -m "fix(core): enforce node inbound ownership at runtime"
```

---

### Task 3: Inbound API Metadata

**Files:**
- Modify: `app/models/proxy.py`
- Modify: `app/routers/system.py`
- Modify: `app/db/crud.py`
- Test: `tests/test_system_inbounds.py`

- [ ] **Step 1: Add failing API/helper test**

Create `tests/test_system_inbounds.py`:

```python
import os
os.environ.setdefault("XRAY_EXECUTABLE_PATH", "/bin/echo")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import xray
from app.db.base import Base
from app.db.models import Node, ProxyInbound
from app.models.node import NodeInboundsMode
from app.routers.system import build_inbounds_response
from app.xray.config import XRayConfig


def test_build_inbounds_response_includes_owner_node_id(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    config = XRayConfig({
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": "node-1-vless-443", "protocol": "vless", "port": 443}],
        "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
    })
    monkeypatch.setattr(xray, "config", config)

    with TestingSession() as db:
        node = Node(name="n1", address="203.0.113.1", port=62050, api_port=62051, inbounds_mode=NodeInboundsMode.panel)
        db.add(node)
        db.flush()
        db.add(ProxyInbound(tag="node-1-vless-443", owner_node_id=node.id))
        db.commit()
        response = build_inbounds_response(db)

    inbound = response["vless"][0]
    assert inbound["owner_node_id"] == 1
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python -m pytest tests/test_system_inbounds.py -q
```

Expected: FAIL because `build_inbounds_response` does not exist.

- [ ] **Step 3: Add Pydantic field**

In `app/models/proxy.py`:

```python
owner_node_id: Union[int, None] = None
```

Add it to `ProxyInbound` after `port`.

- [ ] **Step 4: Add response builder**

In `app/routers/system.py`:

```python
def build_inbounds_response(db: Session) -> Dict[ProxyTypes, List[dict]]:
    tags = [
        inbound["tag"]
        for inbound_list in xray.config.inbounds_by_protocol.values()
        for inbound in inbound_list
    ]
    owner_ids = crud.get_inbound_owner_ids(db, tags)
    return {
        protocol: [
            {**inbound, "owner_node_id": owner_ids.get(inbound["tag"])}
            for inbound in inbound_list
        ]
        for protocol, inbound_list in xray.config.inbounds_by_protocol.items()
    }
```

Change endpoint:

```python
@router.get("/inbounds", response_model=Dict[ProxyTypes, List[ProxyInbound]])
def get_inbounds(db: Session = Depends(get_db), admin: Admin = Depends(Admin.get_current)):
    return build_inbounds_response(db)
```

- [ ] **Step 5: Run tests and commit**

Run:

```bash
python -m pytest tests/test_system_inbounds.py tests/test_node_active_inbounds.py -q
```

Commit:

```bash
git add app/models/proxy.py app/routers/system.py app/db/crud.py tests/test_system_inbounds.py
git commit -m "feat(api): expose inbound node ownership"
```

---

### Task 4: Dashboard Node-Owned Inbound UX

**Files:**
- Modify: `app/dashboard/src/contexts/DashboardContext.tsx`
- Modify: `app/dashboard/src/components/NodesModal.tsx`
- Modify: `app/dashboard/public/statics/locales/en.json`
- Modify: `app/dashboard/public/statics/locales/zh.json`
- Modify: `app/dashboard/public/statics/locales/fa.json`
- Modify: `app/dashboard/public/statics/locales/ru.json`

- [ ] **Step 1: Update inbound type**

In `DashboardContext.tsx`, add to `InboundType`:

```ts
owner_node_id?: number | null;
```

- [ ] **Step 2: Filter selectable options without hiding invalid selected values**

In `NodesModal.tsx` inside `NodeForm`, derive current node id:

```ts
const currentNodeId = form.watch("id");
const selectedActiveInbounds = form.watch("active_inbounds") || [];
const isEditingExistingNode = Boolean(currentNodeId);
const initialInboundsModeRef = useRef(form.getValues("inbounds_mode"));
```

Change inbound option derivation:

```ts
const allInboundOptions = useMemo(
  () =>
    Array.from(inbounds.entries())
      .flatMap(([protocol, inboundList]) =>
        inboundList.map((inbound) => ({
          ...inbound,
          protocol,
        }))
      )
      .sort((a, b) => a.tag.localeCompare(b.tag)),
  [inbounds]
);

const inboundOptions = useMemo(
  () => allInboundOptions.filter((inbound) => currentNodeId && inbound.owner_node_id === currentNodeId),
  [allInboundOptions, currentNodeId]
);

const invalidSelectedInbounds = useMemo(
  () =>
    selectedActiveInbounds.filter(
      (tag) => !inboundOptions.some((inbound) => inbound.tag === tag)
    ),
  [selectedActiveInbounds, inboundOptions]
);
```

- [ ] **Step 3: Show invalid selected tags**

Above selectable checkboxes, render an alert if `invalidSelectedInbounds.length > 0`:

```tsx
<Alert status="warning" size="sm">
  <AlertIcon />
  <AlertDescription>
    {t("nodes.invalidOwnedInbounds", { tags: invalidSelectedInbounds.join(", ") })}
  </AlertDescription>
</Alert>
```

Add a small button to remove invalid tags:

```tsx
<Button
  size="xs"
  variant="outline"
  onClick={() => form.setValue(
    "active_inbounds",
    selectedActiveInbounds.filter((tag) => !invalidSelectedInbounds.includes(tag))
  )}
>
  {t("nodes.removeInvalidInbounds")}
</Button>
```

- [ ] **Step 4: Make manual Add Node legacy-only**

For manual add form where `currentNodeId` is missing, show copy instead of empty checkbox list:

```tsx
{!currentNodeId && (
  <Text fontSize="xs" color="gray.500">
    {t("nodes.manualNodeLegacyOnly")}
  </Text>
)}
```

Ensure submit does not derive edit-mode `inbounds_mode` only from `active_inbounds.length`. Existing panel nodes must remain `panel` when invalid tags are removed, and the backend will reject saving a panel node with no owned active inbounds. Manual Add Node remains legacy-only and submits `legacy` with empty `active_inbounds`. A future explicit advanced "convert to legacy/manual" action can be added separately, but this task must not silently convert panel nodes to legacy.

In the submit transform, use behavior equivalent to:

```ts
const resolvedInboundsMode =
  isEditingExistingNode && initialInboundsModeRef.current === "panel"
    ? "panel"
    : values.active_inbounds.length > 0
      ? "panel"
      : "legacy";
```

- [ ] **Step 5: Update locale strings**

Add keys to `en.json`:

```json
"nodes.activeInboundsHint": "Panel-managed nodes can only run inbounds owned by this node. Use Add Node provisioning to create node-owned inbounds.",
"nodes.invalidOwnedInbounds": "These selected inbounds are retained only for cleanup and cannot run on this node. Remove them before saving: {{tags}}",
"nodes.removeInvalidInbounds": "Remove invalid inbounds",
"nodes.manualNodeLegacyOnly": "Manual nodes use legacy node-side inbound selection. Use Add Node provisioning to create panel-managed node-owned inbounds."
```

Add equivalent concise translations to `zh.json`; use English fallback text for `fa.json` and `ru.json` if no translation is available.

- [ ] **Step 6: Build dashboard and commit**

Run:

```bash
bash build_dashboard.sh
```

Commit:

```bash
git add app/dashboard/src/contexts/DashboardContext.tsx app/dashboard/src/components/NodesModal.tsx app/dashboard/public/statics/locales app/dashboard/build
git commit -m "feat(dashboard): restrict node inbound choices to owned inbounds"
```

---

### Task 4.5: Preserve Owned Inbounds During Core Config Updates

**Files:**
- Modify: `app/xray/node_provisioning.py`
- Test: `tests/test_node_provisioning.py`

- [ ] **Step 1: Write failing preservation test**

Add a test showing an owned but currently deselected inbound is still protected from accidental config deletion:

```python
def test_core_config_update_preserves_owned_inbounds_even_when_deselected():
    db = _db_session()
    node = DBNode(name="n1", address="203.0.113.1", port=62050, api_port=62051, inbounds_mode=NodeInboundsMode.panel)
    db.add(node)
    db.flush()
    db.add(ProxyInbound(tag="node-1-vless-443", owner_node_id=node.id))
    db.commit()

    try:
        validate_core_config_preserves_panel_inbounds(
            db,
            {"log": {"loglevel": "warning"}, "inbounds": [], "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}]},
        )
    except ValueError as exc:
        assert "node-1-vless-443" in str(exc)
    else:
        raise AssertionError("expected owned inbound deletion to be rejected")
```

- [ ] **Step 2: Update managed-tag lookup**

Change `_panel_managed_inbound_tags(db)` from active-association lookup to owner lookup:

```python
def _panel_managed_inbound_tags(db: Session) -> set[str]:
    return {
        tag
        for (tag,) in db.query(ProxyInbound.tag)
        .filter(ProxyInbound.owner_node_id.isnot(None))
        .all()
        if tag
    }
```

This protects every panel-owned inbound from being deleted by core config updates, even if the node temporarily deselects it.

- [ ] **Step 3: Run tests and commit**

Run:

```bash
python -m pytest tests/test_node_provisioning.py -q
```

Commit:

```bash
git add app/xray/node_provisioning.py tests/test_node_provisioning.py
git commit -m "fix(core): preserve owned inbounds in config updates"
```

---

### Task 5: Owner-Based Deletion And Cleanup

**Files:**
- Modify: `app/xray/node_provisioning.py`
- Test: `tests/test_node_provisioning.py`

- [ ] **Step 1: Write failing deletion test**

Add test to `tests/test_node_provisioning.py`:

```python
def test_remove_provisioned_node_removes_owned_inbounds_even_when_deselected(monkeypatch):
    import app.xray.node_provisioning as node_provisioning

    db = _db_session()
    node = DBNode(name="n1", address="203.0.113.1", port=62050, api_port=62051, inbounds_mode=NodeInboundsMode.panel)
    db.add(node)
    db.flush()
    other_node = DBNode(name="n2", address="203.0.113.2", port=62050, api_port=62051, inbounds_mode=NodeInboundsMode.panel)
    db.add(other_node)
    db.flush()
    owned = ProxyInbound(tag="node-1-vless-443", owner_node_id=node.id)
    other = ProxyInbound(tag="node-2-vless-443", owner_node_id=other_node.id)
    db.add_all([owned, other])
    db.commit()

    applied = []
    monkeypatch.setattr(node_provisioning.xray, "config", XRayConfig({
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"tag": "node-1-vless-443", "protocol": "vless", "port": 443},
            {"tag": "node-2-vless-443", "protocol": "vless", "port": 443},
        ],
        "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
    }))
    monkeypatch.setattr(node_provisioning, "_apply_provisioned_config", lambda payload: applied.append(payload))

    removed = remove_provisioned_node(db, node)

    assert removed == ["node-1-vless-443"]
    assert db.query(ProxyInbound).filter_by(tag="node-1-vless-443").first() is None
    assert db.query(ProxyInbound).filter_by(tag="node-2-vless-443").first() is not None
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python -m pytest tests/test_node_provisioning.py::test_remove_provisioned_node_removes_owned_inbounds_even_when_deselected -q
```

Expected: FAIL because deletion uses active tag helper.

- [ ] **Step 3: Use owner-based lookup**

In `app/xray/node_provisioning.py`, replace generated tag lookup with DB owner query:

```python
def generated_inbound_tags_for_node(db: Session, dbnode: DBNode) -> list[str]:
    return [
        tag
        for (tag,) in db.query(ProxyInbound.tag)
        .filter(ProxyInbound.owner_node_id == dbnode.id)
        .order_by(ProxyInbound.tag)
        .all()
    ]
```

Update `remove_provisioned_node(db, dbnode)` to call `generated_inbound_tags_for_node(db, dbnode)`.

- [ ] **Step 4: Run tests and commit**

Run:

```bash
python -m pytest tests/test_node_provisioning.py -q
```

Commit:

```bash
git add app/xray/node_provisioning.py tests/test_node_provisioning.py
git commit -m "fix(core): delete inbounds by node ownership"
```

---

### Task 6: Documentation And Final Verification

**Files:**
- Modify: `README.md`
- Modify: `README-zh-cn.md`
- Modify: `docs/node-provisioning.md`
- Modify: `/Users/zheng/Code/MarzbanX-node/README.md`
- Modify: `/Users/zheng/Code/MarzbanX-node/DEPLOYMENT.md`

- [ ] **Step 1: Document node-owned model**

Document these exact model rules:

```text
Users are managed by protocol.
Nodes run only inbounds owned by that node.
Add Node creates node-owned inbounds.
Global inbound reuse is no longer a MarzbanX node-management workflow.
Legacy unowned inbounds are migration candidates and are not valid for panel-managed node runtime selection.
The Rust MarzbanX-node `INBOUNDS` environment variable is legacy/manual mode. New panel-managed deployments should use Add Node provisioning so the controller owns and sends the active inbound list.
Upgrade playbook: after migration, generated tags matching `node-{id}-{protocol}-{port}` are assigned automatically, including `anytls`; any remaining panel node selections shown as invalid must be removed and recreated through Add Node provisioning, or manually assigned an `owner_node_id` only if the inbound really belongs to that node.
```

- [ ] **Step 2: Run final verification**

Run:

```bash
python -m pytest tests/test_node_active_inbounds.py tests/test_node_provisioning.py tests/test_hysteria_support.py tests/test_system_inbounds.py -q
bash build_dashboard.sh
XRAY_EXECUTABLE_PATH=/bin/echo alembic heads
```

Expected: all commands pass; Alembic has one head.

- [ ] **Step 3: Commit docs**

```bash
git add README.md README-zh-cn.md docs/node-provisioning.md
git commit -m "docs: explain node-owned inbound model"
```

Commit node repository docs separately:

```bash
cd /Users/zheng/Code/MarzbanX-node
git add README.md DEPLOYMENT.md
git commit -m "docs: mark INBOUNDS as legacy manual mode"
```

---

## Subagent Review Gates

Before implementation:

- [ ] Revised plan receives no high-risk blocker from data/API review.
- [ ] Revised plan receives no high-risk blocker from UX/backward-compatibility review.
- [ ] Any medium risk is addressed in this plan or explicitly accepted.
- [ ] SQLite migration, strict generated-tag matching including AnyTLS, runtime validation for reused node objects, and edit-mode panel preservation are explicitly included.

During implementation:

- [ ] After Task 1 and Task 2, request focused risk review on migration and validation behavior.
- [ ] After Task 3 and Task 4, request focused review on API shape and UI filtering.
- [ ] Before final status, run verification commands and inspect output.

## Known Non-Goals

- Do not preserve multi-node global inbound reuse as a MarzbanX panel-managed workflow.
- Do not change Rust MarzbanX-node REST protocol unless tests prove controller-sent active inbound tags are insufficient.
- Do not convert user management to per-node manual user assignment.
- Do not automatically delete legacy unowned inbounds in the first implementation.
