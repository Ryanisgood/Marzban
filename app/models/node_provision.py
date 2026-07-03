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
