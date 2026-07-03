import asyncio
import time
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, WebSocket
from sqlalchemy.exc import IntegrityError
from starlette.websockets import WebSocketDisconnect

from app import logger, xray
from app.db import Session, crud, get_db
from app.dependencies import get_dbnode, validate_dates
from app.models.admin import Admin
from app.models.node import (
    NodeCreate,
    NodeInboundsMode,
    NodeModify,
    NodeResponse,
    NodeSettings,
    NodeStatus,
    NodesUsageResponse,
)
from app.models.proxy import ProxyHost
from app.utils import responses
from app.xray.node_status import build_node_runtime_status, get_inbound_user_counts

router = APIRouter(
    tags=["Node"], prefix="/api", responses={401: responses._401, 403: responses._403}
)


def add_host_if_needed(new_node: NodeCreate, db: Session):
    """Add a host if specified in the new node settings."""
    if new_node.add_as_new_host:
        host = ProxyHost(
            remark=f"{new_node.name} ({{USERNAME}}) [{{PROTOCOL}} - {{TRANSPORT}}]",
            address=new_node.address,
        )
        for inbound_tag in xray.config.inbounds_by_tag:
            crud.add_host(db, inbound_tag, host)
        xray.hosts.update()


def validate_active_inbounds(active_inbounds: List[str]):
    unknown_inbounds = [
        inbound for inbound in active_inbounds
        if inbound not in xray.config.inbounds_by_tag
    ]
    if unknown_inbounds:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown inbound tag(s): {', '.join(unknown_inbounds)}",
        )


def validate_inbounds_selection(inbounds_mode: NodeInboundsMode, active_inbounds: List[str]):
    if inbounds_mode == NodeInboundsMode.panel and not active_inbounds:
        raise HTTPException(
            status_code=400,
            detail="At least one active inbound is required in panel mode",
        )
    validate_active_inbounds(active_inbounds)


def node_response(dbnode, inbound_user_counts=None) -> NodeResponse:
    response = NodeResponse.model_validate(dbnode)
    response.runtime_status = build_node_runtime_status(
        dbnode,
        runtime_node=xray.nodes.get(dbnode.id),
        inbound_user_counts=inbound_user_counts,
    )
    return response


@router.get("/node/settings", response_model=NodeSettings)
def get_node_settings(
    db: Session = Depends(get_db), admin: Admin = Depends(Admin.check_sudo_admin)
):
    """Retrieve the current node settings, including TLS certificate."""
    tls = crud.get_tls_certificate(db)
    return NodeSettings(certificate=tls.certificate)


@router.post("/node", response_model=NodeResponse, responses={409: responses._409})
def add_node(
    new_node: NodeCreate,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Add a new node to the database and optionally add it as a host."""
    validate_inbounds_selection(new_node.inbounds_mode, new_node.active_inbounds)
    try:
        dbnode = crud.create_node(db, new_node)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=f'Node "{new_node.name}" already exists'
        )

    bg.add_task(xray.operations.connect_node, node_id=dbnode.id)
    bg.add_task(add_host_if_needed, new_node, db)

    logger.info(f'New node "{dbnode.name}" added')
    return node_response(dbnode)


@router.get("/node/{node_id}", response_model=NodeResponse)
def get_node(
    dbnode: NodeResponse = Depends(get_dbnode),
    db: Session = Depends(get_db),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Retrieve details of a specific node by its ID."""
    return node_response(dbnode, get_inbound_user_counts(db))


@router.websocket("/node/{node_id}/logs")
async def node_logs(node_id: int, websocket: WebSocket, db: Session = Depends(get_db)):
    token = websocket.query_params.get("token") or websocket.headers.get(
        "Authorization", ""
    ).removeprefix("Bearer ")
    admin = Admin.get_admin(token, db)
    if not admin:
        return await websocket.close(reason="Unauthorized", code=4401)

    if not admin.is_sudo:
        return await websocket.close(reason="You're not allowed", code=4403)

    if not xray.nodes.get(node_id):
        return await websocket.close(reason="Node not found", code=4404)

    if not xray.nodes[node_id].connected:
        return await websocket.close(reason="Node is not connected", code=4400)

    interval = websocket.query_params.get("interval")
    if interval:
        try:
            interval = float(interval)
        except ValueError:
            return await websocket.close(reason="Invalid interval value", code=4400)
        if interval > 10:
            return await websocket.close(
                reason="Interval must be more than 0 and at most 10 seconds", code=4400
            )

    await websocket.accept()

    cache = ""
    last_sent_ts = 0
    node = xray.nodes[node_id]
    with node.get_logs() as logs:
        while True:
            if not node == xray.nodes[node_id]:
                break

            if interval and time.time() - last_sent_ts >= interval and cache:
                try:
                    await websocket.send_text(cache)
                except (WebSocketDisconnect, RuntimeError):
                    break
                cache = ""
                last_sent_ts = time.time()

            if not logs:
                try:
                    await asyncio.wait_for(websocket.receive(), timeout=0.2)
                    continue
                except asyncio.TimeoutError:
                    continue
                except (WebSocketDisconnect, RuntimeError):
                    break

            log = logs.popleft()

            if interval:
                cache += f"{log}\n"
                continue

            try:
                await websocket.send_text(log)
            except (WebSocketDisconnect, RuntimeError):
                break


@router.get("/nodes", response_model=List[NodeResponse])
def get_nodes(
    db: Session = Depends(get_db), _: Admin = Depends(Admin.check_sudo_admin)
):
    """Retrieve a list of all nodes. Accessible only to sudo admins."""
    inbound_user_counts = get_inbound_user_counts(db)
    return [
        node_response(dbnode, inbound_user_counts)
        for dbnode in crud.get_nodes(db)
    ]


@router.put("/node/{node_id}", response_model=NodeResponse)
def modify_node(
    modified_node: NodeModify,
    bg: BackgroundTasks,
    dbnode: NodeResponse = Depends(get_dbnode),
    db: Session = Depends(get_db),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Update a node's details. Only accessible to sudo admins."""
    connection_settings_changed = (
        (modified_node.address is not None and modified_node.address != dbnode.address)
        or (modified_node.port is not None and modified_node.port != dbnode.port)
        or (modified_node.api_port is not None and modified_node.api_port != dbnode.api_port)
        or (
            modified_node.usage_coefficient is not None
            and modified_node.usage_coefficient != dbnode.usage_coefficient
        )
    )
    was_disabled = dbnode.status == NodeStatus.disabled

    active_inbounds = (
        modified_node.active_inbounds
        if modified_node.active_inbounds is not None
        else dbnode.active_inbounds
    )
    inbounds_mode = modified_node.inbounds_mode or dbnode.inbounds_mode
    validate_inbounds_selection(inbounds_mode, active_inbounds)

    updated_node = crud.update_node(db, dbnode, modified_node)
    if updated_node.status == NodeStatus.disabled:
        xray.operations.remove_node(updated_node.id)
    elif connection_settings_changed or was_disabled:
        xray.operations.remove_node(updated_node.id)
        bg.add_task(xray.operations.connect_node, node_id=updated_node.id)
    else:
        bg.add_task(xray.operations.restart_node, node_id=updated_node.id)

    logger.info(f'Node "{dbnode.name}" modified')
    return node_response(updated_node, get_inbound_user_counts(db))


@router.post("/node/{node_id}/reconnect")
def reconnect_node(
    bg: BackgroundTasks,
    dbnode: NodeResponse = Depends(get_dbnode),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Trigger a reconnection for the specified node. Only accessible to sudo admins."""
    bg.add_task(xray.operations.connect_node, node_id=dbnode.id)
    return {"detail": "Reconnection task scheduled"}


@router.delete("/node/{node_id}")
def remove_node(
    dbnode: NodeResponse = Depends(get_dbnode),
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.check_sudo_admin),
):
    """Delete a node and remove it from xray in the background."""
    crud.remove_node(db, dbnode)
    xray.operations.remove_node(dbnode.id)

    logger.info(f'Node "{dbnode.name}" deleted')
    return {}


@router.get("/nodes/usage", response_model=NodesUsageResponse)
def get_usage(
    db: Session = Depends(get_db),
    start: str = "",
    end: str = "",
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Retrieve usage statistics for nodes within a specified date range."""
    start, end = validate_dates(start, end)

    usages = crud.get_nodes_usage(db, start, end)

    return {"usages": usages}
