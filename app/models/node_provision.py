from enum import Enum
from typing import Dict, List

from pydantic import BaseModel, Field

from app.models.node import NodeResponse


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


class NodeProvisionResponse(BaseModel):
    node: NodeResponse
    active_inbounds: List[str]
    core_kind: str
    install_token: str
    install_command: str


class NodeProvisionRedeemRequest(BaseModel):
    token: str


class NodeInstallPayload(BaseModel):
    node_id: int
    node_name: str
    service_port: int
    api_port: int
    active_inbounds: List[str]
    core_kind: str
    ssl_client_cert: str
    binary_url: str = ""
    core_install_url: str = ""
    env: Dict[str, str]
