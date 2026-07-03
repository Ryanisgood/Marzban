from collections import defaultdict
from typing import Dict, Iterable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import xray
from app.db import models as db_models
from app.models.node import (
    NodeInboundRuntimeDetail,
    NodeInboundsMode,
    NodeRuntimeStatus,
)
from app.models.proxy import ProxyTypes
from app.models.user import UserStatus


def _normalize_core_kind(core_kind: Optional[str]) -> Optional[str]:
    if not core_kind:
        return None
    if core_kind in {"xray", "sing-box"}:
        return core_kind
    return "unknown"


def _requires_sing_box(inbound: dict) -> bool:
    return (
        inbound.get("protocol") == ProxyTypes.Hysteria
        or inbound.get("protocol") == ProxyTypes.Hysteria.value
    ) and inbound.get("network") == "hysteria"


def _protocol_value(inbound: dict) -> str:
    protocol = inbound.get("protocol")
    return getattr(protocol, "value", protocol) or ""


def _expected_core_for_inbounds(inbounds: Iterable[dict]) -> tuple[Optional[str], str]:
    inbounds = list(inbounds)
    if not inbounds:
        return None, "No active inbounds are selected"

    sing_box_tags = [
        inbound.get("tag")
        for inbound in inbounds
        if _requires_sing_box(inbound)
    ]
    if sing_box_tags:
        return "sing-box", f"INBOUNDS contains hysteria2: {', '.join(sing_box_tags)}"

    return "xray", "All active inbounds are Xray-compatible"


def _public_ports(inbound: dict) -> list:
    ports = []
    for host in xray.hosts.get(inbound["tag"], []):
        port = host.get("port") or inbound.get("port")
        if port is not None and port not in ports:
            ports.append(port)

    if not ports and inbound.get("port") is not None:
        ports.append(inbound.get("port"))

    return ports


def build_node_runtime_status(
    dbnode,
    *,
    runtime_node=None,
    inbound_user_counts: Optional[Dict[str, int]] = None,
) -> NodeRuntimeStatus:
    inbound_user_counts = inbound_user_counts or {}
    actual_core = _normalize_core_kind(getattr(runtime_node, "core_kind", None))
    xray_api_available = getattr(runtime_node, "xray_api_available", None)
    diagnostics = {
        "node_version": getattr(runtime_node, "node_version", None),
        "installed_cores": getattr(runtime_node, "installed_cores", None) or {},
        "memory": getattr(runtime_node, "memory", None) or {},
        "local_listening_ports": getattr(runtime_node, "local_listening_ports", None) or [],
        "configured_inbound_ports": getattr(runtime_node, "configured_inbound_ports", None) or [],
        "last_core_restart_at": getattr(runtime_node, "last_core_restart_at", None),
    }

    if dbnode.inbounds_mode != NodeInboundsMode.panel:
        return NodeRuntimeStatus(
            actual_core=actual_core,
            core_reason="Legacy INBOUNDS mode; controller does not own this node's inbound selection",
            xray_api_available=xray_api_available,
            restart_required=False,
            **diagnostics,
        )

    active_tags = list(getattr(dbnode, "active_inbounds", []) or [])
    unknown_tags = [
        tag for tag in active_tags
        if tag not in xray.config.inbounds_by_tag
    ]
    active_inbounds = [
        xray.config.inbounds_by_tag[tag]
        for tag in active_tags
        if tag in xray.config.inbounds_by_tag
    ]
    if unknown_tags:
        expected_core, reason = (
            None,
            f"Unknown active inbound(s): {', '.join(unknown_tags)}",
        )
    else:
        expected_core, reason = _expected_core_for_inbounds(active_inbounds)

    details = []
    for tag in unknown_tags:
        details.append(
            NodeInboundRuntimeDetail(
                tag=tag,
                protocol="unknown",
            )
        )

    for inbound in active_inbounds:
        public_ports = _public_ports(inbound)
        details.append(
            NodeInboundRuntimeDetail(
                tag=inbound["tag"],
                protocol=_protocol_value(inbound),
                network=inbound.get("network"),
                tls=inbound.get("tls"),
                port=inbound.get("port"),
                public_port=public_ports[0] if public_ports else None,
                public_ports=public_ports,
                users_count=inbound_user_counts.get(inbound["tag"], 0),
            )
        )

    last_started_inbounds = getattr(runtime_node, "last_started_inbounds", None)
    restart_required = expected_core is not None and runtime_node is None
    if runtime_node is not None:
        if last_started_inbounds is not None:
            restart_required = set(last_started_inbounds) != set(active_tags)
        if expected_core is not None and actual_core is None:
            restart_required = True
        if actual_core is not None and expected_core is not None:
            restart_required = restart_required or actual_core != expected_core

    return NodeRuntimeStatus(
        active_inbounds_details=details,
        expected_core=expected_core,
        actual_core=actual_core,
        core_reason=reason,
        xray_api_available=xray_api_available,
        restart_required=restart_required,
        **diagnostics,
    )


def get_inbound_user_counts(db: Session) -> Dict[str, int]:
    query = (
        db.query(
            db_models.User.id,
            func.lower(db_models.Proxy.type).label("type"),
            func.group_concat(
                db_models.excluded_inbounds_association.c.inbound_tag
            ).label("excluded_inbound_tags"),
        )
        .join(db_models.Proxy, db_models.User.id == db_models.Proxy.user_id)
        .outerjoin(
            db_models.excluded_inbounds_association,
            db_models.Proxy.id == db_models.excluded_inbounds_association.c.proxy_id,
        )
        .filter(db_models.User.status.in_([UserStatus.active, UserStatus.on_hold]))
        .group_by(
            db_models.User.id,
            func.lower(db_models.Proxy.type),
        )
    )

    counts = defaultdict(int)
    for row in query.all():
        excluded = set(row.excluded_inbound_tags.split(",")) if row.excluded_inbound_tags else set()
        for inbound in xray.config.inbounds_by_protocol.get(row.type, []):
            tag = inbound["tag"]
            if tag not in excluded:
                counts[tag] += 1

    return dict(counts)
