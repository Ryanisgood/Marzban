from functools import lru_cache
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from app import logger, xray
from app.db import GetDB, crud
from app.models.node import NodeInboundsMode, NodeStatus
from app.models.proxy import ProxyTypes
from app.models.user import UserResponse
from app.utils.concurrency import threaded_function
from app.xray.node import XRayNode
from xray_api import XRay as XRayAPI
from xray_api.types.account import Account, XTLSFlows

if TYPE_CHECKING:
    from app.db import User as DBUser
    from app.db.models import Node as DBNode


@lru_cache(maxsize=None)
def get_tls():
    from app.db import GetDB, get_tls_certificate
    with GetDB() as db:
        tls = get_tls_certificate(db)
        return {
            "key": tls.key,
            "certificate": tls.certificate
        }


def _node_active_inbounds(dbnode: "DBNode"):
    return (
        dbnode.active_inbounds
        if dbnode.inbounds_mode == NodeInboundsMode.panel
        else None
    )


def _node_runs_inbound(node, inbound_tag: str) -> bool:
    active_inbounds = getattr(node, "active_inbounds", None)
    return active_inbounds is None or inbound_tag in active_inbounds


def _node_api_or_none(node):
    if not node.connected or not node.started:
        return None
    try:
        return node.api
    except (ConnectionError, xray.exc.ConnectionError):
        return None


def _node_apis_for_inbound(nodes, inbound_tag: str):
    for node in list(nodes):
        if not _node_runs_inbound(node, inbound_tag):
            continue
        api = _node_api_or_none(node)
        if api:
            yield api


def _hysteria_inbound_tags_from_user(dbuser: "DBUser") -> set[str]:
    try:
        user = UserResponse.model_validate(dbuser)
        return set(user.inbounds.get(ProxyTypes.Hysteria, []))
    except Exception:
        return {
            inbound["tag"]
            for inbound in xray.config.inbounds_by_protocol.get(ProxyTypes.Hysteria, [])
        }


@threaded_function
def _add_user_to_inbound(api: XRayAPI, inbound_tag: str, account: Account):
    try:
        api.add_inbound_user(tag=inbound_tag, user=account, timeout=30)
    except (xray.exc.EmailExistsError, xray.exc.ConnectionError):
        pass


@threaded_function
def _remove_user_from_inbound(api: XRayAPI, inbound_tag: str, email: str):
    try:
        api.remove_inbound_user(tag=inbound_tag, email=email, timeout=30)
    except (xray.exc.EmailNotFoundError, xray.exc.ConnectionError):
        pass


@threaded_function
def _alter_inbound_user(api: XRayAPI, inbound_tag: str, account: Account):
    try:
        api.remove_inbound_user(tag=inbound_tag, email=account.email, timeout=30)
    except (xray.exc.EmailNotFoundError, xray.exc.ConnectionError):
        pass
    try:
        api.add_inbound_user(tag=inbound_tag, user=account, timeout=30)
    except (xray.exc.EmailExistsError, xray.exc.ConnectionError):
        pass


def add_user(dbuser: "DBUser"):
    user = UserResponse.model_validate(dbuser)
    email = f"{dbuser.id}.{dbuser.username}"
    config_reload_inbounds = set()

    for proxy_type, inbound_tags in user.inbounds.items():
        if proxy_type == ProxyTypes.Hysteria:
            config_reload_inbounds.update(inbound_tags)
        for inbound_tag in inbound_tags:
            inbound = xray.config.inbounds_by_tag.get(inbound_tag, {})

            try:
                proxy_settings = user.proxies[proxy_type].dict(no_obj=True)
            except KeyError:
                pass
            account = proxy_type.account_model(email=email, **proxy_settings)

            # XTLS currently only supports transmission methods of TCP and mKCP
            if getattr(account, 'flow', None) and (
                inbound.get('network', 'tcp') not in ('tcp', 'kcp')
                or
                (
                    inbound.get('network', 'tcp') in ('tcp', 'kcp')
                    and
                    inbound.get('tls') not in ('tls', 'reality')
                )
                or
                inbound.get('header_type') == 'http'
            ):
                account.flow = XTLSFlows.NONE

            _add_user_to_inbound(xray.api, inbound_tag, account)  # main core
            for node_api in _node_apis_for_inbound(xray.nodes.values(), inbound_tag):
                _add_user_to_inbound(node_api, inbound_tag, account)

    if config_reload_inbounds:
        _restart_started_nodes_for_config_reload(config_reload_inbounds)


def remove_user(dbuser: "DBUser", config_reload_inbounds=None):
    email = f"{dbuser.id}.{dbuser.username}"
    if config_reload_inbounds is None:
        config_reload_inbounds = (
            _hysteria_inbound_tags_from_user(dbuser)
            if any(proxy.type == ProxyTypes.Hysteria for proxy in dbuser.proxies)
            else set()
        )
    else:
        config_reload_inbounds = set(config_reload_inbounds)

    for inbound_tag in xray.config.inbounds_by_tag:
        _remove_user_from_inbound(xray.api, inbound_tag, email)
        for node_api in _node_apis_for_inbound(xray.nodes.values(), inbound_tag):
            _remove_user_from_inbound(node_api, inbound_tag, email)

    if config_reload_inbounds:
        _restart_started_nodes_for_config_reload(config_reload_inbounds)


def update_user(dbuser: "DBUser", config_reload_inbounds=None):
    user = UserResponse.model_validate(dbuser)
    email = f"{dbuser.id}.{dbuser.username}"
    if config_reload_inbounds is None:
        config_reload_inbounds = set(user.inbounds.get(ProxyTypes.Hysteria, []))
        if not config_reload_inbounds and xray.config.inbounds_by_protocol.get(ProxyTypes.Hysteria):
            config_reload_inbounds = {
                inbound["tag"]
                for inbound in xray.config.inbounds_by_protocol.get(ProxyTypes.Hysteria, [])
            }
    else:
        config_reload_inbounds = set(config_reload_inbounds)

    active_inbounds = []
    for proxy_type, inbound_tags in user.inbounds.items():
        for inbound_tag in inbound_tags:
            active_inbounds.append(inbound_tag)
            inbound = xray.config.inbounds_by_tag.get(inbound_tag, {})

            try:
                proxy_settings = user.proxies[proxy_type].dict(no_obj=True)
            except KeyError:
                pass
            account = proxy_type.account_model(email=email, **proxy_settings)

            # XTLS currently only supports transmission methods of TCP and mKCP
            if getattr(account, 'flow', None) and (
                inbound.get('network', 'tcp') not in ('tcp', 'kcp')
                or
                (
                    inbound.get('network', 'tcp') in ('tcp', 'kcp')
                    and
                    inbound.get('tls') not in ('tls', 'reality')
                )
                or
                inbound.get('header_type') == 'http'
            ):
                account.flow = XTLSFlows.NONE

            _alter_inbound_user(xray.api, inbound_tag, account)  # main core
            for node_api in _node_apis_for_inbound(xray.nodes.values(), inbound_tag):
                _alter_inbound_user(node_api, inbound_tag, account)

    for inbound_tag in xray.config.inbounds_by_tag:
        if inbound_tag in active_inbounds:
            continue
        # remove disabled inbounds
        _remove_user_from_inbound(xray.api, inbound_tag, email)
        for node_api in _node_apis_for_inbound(xray.nodes.values(), inbound_tag):
            _remove_user_from_inbound(node_api, inbound_tag, email)

    if config_reload_inbounds:
        _restart_started_nodes_for_config_reload(config_reload_inbounds)


def _restart_started_nodes_for_config_reload(inbound_tags=None):
    inbound_tags = set(inbound_tags or [])
    for node_id, node in list(xray.nodes.items()):
        if not node.connected or not node.started:
            continue
        if inbound_tags and not any(_node_runs_inbound(node, tag) for tag in inbound_tags):
            continue
        restart_node(node_id)


def remove_node(node_id: int):
    if node_id in xray.nodes:
        try:
            xray.nodes[node_id].disconnect()
        except Exception:
            pass
        finally:
            try:
                del xray.nodes[node_id]
            except KeyError:
                pass


def add_node(dbnode: "DBNode"):
    remove_node(dbnode.id)

    tls = get_tls()
    xray.nodes[dbnode.id] = XRayNode(address=dbnode.address,
                                     port=dbnode.port,
                                     api_port=dbnode.api_port,
                                     ssl_key=tls['key'],
                                     ssl_cert=tls['certificate'],
                                     usage_coefficient=dbnode.usage_coefficient,
                                     active_inbounds=_node_active_inbounds(dbnode))

    return xray.nodes[dbnode.id]


def _change_node_status(node_id: int, status: NodeStatus, message: str = None, version: str = None):
    with GetDB() as db:
        try:
            dbnode = crud.get_node_by_id(db, node_id)
            if not dbnode:
                return

            if dbnode.status == NodeStatus.disabled:
                remove_node(dbnode.id)
                return

            crud.update_node_status(db, dbnode, status, message, version)
        except SQLAlchemyError:
            db.rollback()


global _connecting_nodes
_connecting_nodes = {}


@threaded_function
def connect_node(node_id, config=None):
    global _connecting_nodes

    if _connecting_nodes.get(node_id):
        return

    with GetDB() as db:
        dbnode = crud.get_node_by_id(db, node_id)

    if not dbnode:
        return

    try:
        node = xray.nodes[dbnode.id]
        assert node.connected
    except (KeyError, AssertionError):
        node = xray.operations.add_node(dbnode)

    try:
        _connecting_nodes[node_id] = True

        _change_node_status(node_id, NodeStatus.connecting)
        logger.info(f"Connecting to \"{dbnode.name}\" node")

        if config is None:
            config = xray.config.include_db_users()

        node.start(config)
        version = node.get_version()
        _change_node_status(node_id, NodeStatus.connected, version=version)
        with GetDB() as db:
            crud.redeem_node_provision_tokens_for_node(db, node_id)
        logger.info(f"Connected to \"{dbnode.name}\" node, xray run on v{version}")

    except Exception as e:
        _change_node_status(node_id, NodeStatus.error, message=str(e))
        logger.info(f"Unable to connect to \"{dbnode.name}\" node")

    finally:
        try:
            del _connecting_nodes[node_id]
        except KeyError:
            pass


@threaded_function
def restart_node(node_id, config=None):
    with GetDB() as db:
        dbnode = crud.get_node_by_id(db, node_id)

    if not dbnode:
        return

    try:
        node = xray.nodes[dbnode.id]
        node.active_inbounds = _node_active_inbounds(dbnode)
    except KeyError:
        node = xray.operations.add_node(dbnode)

    if not node.connected:
        return connect_node(node_id, config)

    try:
        logger.info(f"Restarting Xray core of \"{dbnode.name}\" node")

        if config is None:
            config = xray.config.include_db_users()

        node.restart(config)
        logger.info(f"Xray core of \"{dbnode.name}\" node restarted")
    except Exception as e:
        _change_node_status(node_id, NodeStatus.error, message=str(e))
        logger.info(f"Unable to restart node {node_id}")
        try:
            node.disconnect()
        except Exception:
            pass


__all__ = [
    "add_user",
    "remove_user",
    "add_node",
    "remove_node",
    "connect_node",
    "restart_node",
]
