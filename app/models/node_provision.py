from enum import Enum
from typing import Dict, List

from pydantic import BaseModel, Field, field_validator

from app.models.node import NodeResponse


class NodeProvisionProtocol(str, Enum):
    hy2 = "hy2"
    anytls = "anytls"
    vless_reality = "vless-reality"
    shadowsocks = "shadowsocks"


class NodeProvisionInbound(BaseModel):
    protocol: NodeProvisionProtocol
    port: int = Field(ge=1, le=65535)
    reality_server_name: str | None = None

    @field_validator("reality_server_name")
    @classmethod
    def normalize_reality_server_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class NodeProvisionCreate(BaseModel):
    name: str
    address: str
    port: int = Field(default=62050, ge=1, le=65535)
    api_port: int = Field(default=62051, ge=1, le=65535)
    usage_coefficient: float = Field(default=1.0, gt=0)
    inbounds: List[NodeProvisionInbound] = Field(min_length=1)

    @field_validator("inbounds")
    @classmethod
    def validate_unique_protocol_ports(
        cls, inbounds: List[NodeProvisionInbound]
    ) -> List[NodeProvisionInbound]:
        seen = set()
        for inbound in inbounds:
            key = (inbound.protocol, inbound.port)
            if key in seen:
                raise ValueError(
                    f"Duplicate protocol/port selection: {inbound.protocol.value}:{inbound.port}"
                )
            seen.add(key)
        return inbounds


class NodeProvisionResponse(BaseModel):
    node: NodeResponse
    active_inbounds: List[str]
    core_kind: str
    install_token: str
    install_command: str


class NodeProvisionRedeemRequest(BaseModel):
    token: str
    consume: bool = False


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
    core_version: str = ""
    core_download_url_template: str = ""
    env: Dict[str, str]
