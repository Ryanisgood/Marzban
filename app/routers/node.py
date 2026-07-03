import asyncio
import time
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, WebSocket
from fastapi.responses import PlainTextResponse
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
from app.models.node_provision import (
    NodeInstallPayload,
    NodeProvisionCreate,
    NodeProvisionRedeemRequest,
    NodeProvisionResponse,
)
from app.models.proxy import ProxyHost
from app.utils import responses
from app.xray.node_provisioning import (
    apply_provisioned_config,
    provision_node,
    redeem_node_install_payload,
    remove_provisioned_node,
    render_node_install_script,
)
from app.xray.node_status import build_node_runtime_status, get_inbound_user_counts

router = APIRouter(
    tags=["Node"], prefix="/api", responses={401: responses._401, 403: responses._403}
)

SING_BOX_SUPPORTED_PROTOCOLS = {"hysteria", "vless", "shadowsocks", "trojan"}
WILDCARD_BINDS = {"0.0.0.0", "::", ""}


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


def _host_address_values(host: dict):
    address = host.get("address")
    if isinstance(address, list):
        return [item for item in address if item]
    if address:
        return [address]
    return []


def _has_usable_host(inbound_tag: str) -> bool:
    return any(
        _host_address_values(host)
        for host in xray.hosts.get(inbound_tag, [])
    )


def _inbound_transport(inbound: dict, raw_inbound: Optional[dict] = None) -> str:
    protocol = inbound.get("protocol")
    network = inbound.get("network")
    if protocol == "hysteria" or network in {"hysteria", "kcp", "quic"}:
        return "udp"
    if raw_inbound and raw_inbound.get("protocol") == "dokodemo-door":
        return "tcp"
    return "tcp"


def _inbound_bind(raw_inbound: Optional[dict]) -> str:
    listen = (raw_inbound or {}).get("listen") or "0.0.0.0"
    if isinstance(listen, dict):
        return listen.get("address") or "0.0.0.0"
    return listen


def _binds_conflict(left: str, right: str) -> bool:
    if left == right:
        return True
    return left in WILDCARD_BINDS or right in WILDCARD_BINDS


def _required_core_for_tags(active_inbounds: List[str]) -> str:
    for tag in active_inbounds:
        inbound = xray.config.inbounds_by_tag.get(tag)
        if (
            inbound
            and inbound.get("protocol") == "hysteria"
            and inbound.get("network") == "hysteria"
        ):
            return "sing-box"
    return "xray"


def _core_installed(runtime_node, core: str) -> Optional[bool]:
    if runtime_node is None:
        return None
    installed_cores = getattr(runtime_node, "installed_cores", None) or {}
    core_info = installed_cores.get(core)
    if core_info is None:
        return None
    if isinstance(core_info, dict):
        return bool(core_info.get("installed"))
    return bool(getattr(core_info, "installed", None))


def _validate_no_port_conflicts(active_inbounds: List[str]):
    seen = []
    for tag in active_inbounds:
        inbound = xray.config.inbounds_by_tag.get(tag)
        raw_inbound = xray.config.get_inbound(tag)
        if not inbound:
            continue
        port = inbound.get("port")
        if not isinstance(port, int):
            continue

        transport = _inbound_transport(inbound, raw_inbound)
        bind = _inbound_bind(raw_inbound)
        for seen_transport, seen_bind, seen_port, seen_tag in seen:
            if (
                seen_transport == transport
                and seen_port == port
                and _binds_conflict(seen_bind, bind)
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Inbound port conflict: "
                        f"{seen_tag} and {tag} both use {transport} {bind}:{port}"
                    ),
                )
        seen.append((transport, bind, port, tag))


def _validate_sing_box_supported_inbounds(active_inbounds: List[str]):
    unsupported_tags = []
    for tag in active_inbounds:
        inbound = xray.config.inbounds_by_tag.get(tag) or {}
        protocol = str(inbound.get("protocol") or "")
        if protocol not in SING_BOX_SUPPORTED_PROTOCOLS:
            unsupported_tags.append(tag)

    if unsupported_tags:
        raise HTTPException(
            status_code=400,
            detail=(
                "sing-box cannot run selected inbound tag(s): "
                f"{', '.join(unsupported_tags)}"
            ),
        )


def validate_inbounds_selection(
    inbounds_mode: NodeInboundsMode,
    active_inbounds: List[str],
    *,
    runtime_node=None,
    require_hosts: bool = True,
):
    if inbounds_mode == NodeInboundsMode.panel and not active_inbounds:
        raise HTTPException(
            status_code=400,
            detail="At least one active inbound is required in panel mode",
        )
    validate_active_inbounds(active_inbounds)
    if inbounds_mode != NodeInboundsMode.panel:
        return []

    if require_hosts:
        missing_hosts = [
            inbound_tag for inbound_tag in active_inbounds
            if not _has_usable_host(inbound_tag)
        ]
        if missing_hosts:
            raise HTTPException(
                status_code=400,
                detail=f"Missing host for inbound tag(s): {', '.join(missing_hosts)}",
            )

    _validate_no_port_conflicts(active_inbounds)

    required_core = _required_core_for_tags(active_inbounds)
    if required_core == "sing-box":
        _validate_sing_box_supported_inbounds(active_inbounds)
    installed = _core_installed(runtime_node, required_core)
    if installed is False:
        raise HTTPException(
            status_code=400,
            detail=f"Required core {required_core} is not installed on this node",
        )

    warnings = []
    if runtime_node is None:
        warnings.append("Node runtime status is unavailable; core installation cannot be verified")
    elif installed is None:
        warnings.append(f"Node did not report {required_core} installation status")
    warnings.append("Public firewall/reachability status is unknown; verify the inbound ports externally")
    return warnings


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
    validate_inbounds_selection(
        new_node.inbounds_mode,
        new_node.active_inbounds,
        require_hosts=not new_node.add_as_new_host,
    )
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


@router.post(
    "/node/provision",
    response_model=NodeProvisionResponse,
    responses={409: responses._409},
)
def provision_new_node(
    payload: NodeProvisionCreate,
    request: Request,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.check_sudo_admin),
):
    """Provision a panel-managed node with generated inbounds and an install command."""
    try:
        result = provision_node(
            db,
            payload,
            admin_username=admin.username,
            controller_url=str(request.base_url).rstrip("/"),
            current_config=xray.config.copy(),
            current_config_provider=lambda: xray.config.copy(),
            apply_config=apply_provisioned_config,
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=f'Node "{payload.name}" already exists'
        )
    except ValueError as err:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(err))

    bg.add_task(xray.operations.connect_node, node_id=result.node.id)
    logger.info(f'Provisioned node "{result.node.name}"')
    return NodeProvisionResponse(
        node=node_response(result.node),
        active_inbounds=result.active_inbounds,
        core_kind=result.core_kind,
        install_token=result.install_token,
        install_command=result.install_command,
    )


@router.get("/node/install.sh", response_class=PlainTextResponse)
def get_node_install_script(request: Request):
    """Return the public node installer shell script."""
    return PlainTextResponse(
        render_node_install_script(str(request.base_url).rstrip("/")),
        media_type="text/x-shellscript",
    )


@router.post("/node/provision/redeem", response_model=NodeInstallPayload)
def redeem_node_provision(
    payload: NodeProvisionRedeemRequest,
    db: Session = Depends(get_db),
):
    """Redeem a one-time provisioning token for node install settings."""
    if payload.consume:
        raise HTTPException(
            status_code=400,
            detail="Install tokens are finalized by controller after node connection",
        )
    result = redeem_node_install_payload(db, payload.token, consume=False)
    if not result:
        raise HTTPException(status_code=403, detail="Invalid or expired install token")
    return result


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
    validate_inbounds_selection(
        inbounds_mode,
        active_inbounds,
        runtime_node=xray.nodes.get(dbnode.id),
    )

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
    remove_provisioned_node(db, dbnode)
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
