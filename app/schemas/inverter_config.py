from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Register(Strict):
    address: int = Field(ge=0, le=65535)
    encoding: Literal['u16', 's16', 'u32', 's32']
    function: Literal[3, 4]
    scale: Decimal = Field(gt=0)
    unit: str = Field(min_length=1, max_length=24)
    writable: bool = False
    minimum: Decimal
    maximum: Decimal

    @model_validator(mode='after')
    def valid_bounds(self):
        if self.minimum > self.maximum or (self.writable and self.function != 3):
            raise ValueError('Invalid limits or writable input register')
        if self.address == 65535 and self.encoding.endswith('32'):
            raise ValueError('Register pair exceeds address space')
        return self


class Profile(Strict):
    manufacturer: Literal['DEYE'] = 'DEYE'
    model: str = Field(min_length=1, max_length=100)
    firmware: list[str] = Field(min_length=1, max_length=32)
    source: str = Field(min_length=1, max_length=500)
    protocol_revision: str = Field(min_length=1, max_length=100)
    word_order: Literal['big', 'little'] = 'big'
    registers: dict[str, Register] = Field(min_length=1, max_length=128)

    @field_validator('registers')
    @classmethod
    def safe_names(cls, value):
        import re
        for name in value:
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name):
                raise ValueError('Invalid setting name')
        return value


class Connection(Strict):
    port: str = Field(pattern=r'^/dev/serial/by-id/[A-Za-z0-9_.:+-]+$', max_length=250)
    device_id: int = Field(ge=1, le=247)
    baudrate: Literal[1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]
    parity: Literal['N', 'E', 'O']
    stopbits: Literal[1, 2]
    sample_seconds: int = Field(ge=5, le=3600)


class DesiredRequest(Strict):
    request_id: uuid.UUID
    expected_version: int = Field(ge=0)
    profile_id: uuid.UUID
    connection: Connection
    settings: dict[str, Decimal] = Field(default_factory=dict, max_length=128)
    reason: str = Field(min_length=1, max_length=500)
    ttl_seconds: int = Field(default=300, ge=30, le=900)


class ReportRequest(Strict):
    request_id: uuid.UUID
    base_version: int = Field(ge=0)
    desired_version: int = Field(ge=1)
    profile_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    kind: Literal['snapshot', 'delta'] = 'snapshot'
    model: str = Field(min_length=1, max_length=100)
    firmware: str = Field(min_length=1, max_length=100)
    measured_at: datetime
    settings: dict[str, Decimal | None] = Field(max_length=128)
    outcome: Literal['observed', 'rejected', 'failed'] = 'observed'
    reason: str | None = Field(default=None, max_length=500)

    @field_validator('measured_at')
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Timezone required')
        return value
