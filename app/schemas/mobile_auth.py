"""Versioned user authentication contract for native clients."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

Password = Annotated[str, Field(min_length=10, max_length=128)]
Secret = Annotated[str, Field(min_length=32, max_length=128)]


class MobileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class EmailInput(MobileInput):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower().strip()


class Installation(MobileInput):
    installation_id: UUID
    installation_key: Secret


class LoginInput(Installation, EmailInput):
    password: Annotated[str, Field(min_length=1, max_length=128)]
    device_name: Annotated[str, Field(min_length=1, max_length=80)]
    platform: Literal["ios", "android", "web"]


class VerifySignupInput(LoginInput):
    password: Password
    full_name: Annotated[str, Field(min_length=1, max_length=200)]
    code: Annotated[str, Field(pattern=r"^\d{10}$")]


class RefreshInput(Installation):
    refresh_token: Secret


class RevokeInput(MobileInput):
    password: Annotated[str, Field(min_length=1, max_length=128)]


class MobileUser(BaseModel):
    id: UUID
    email: str
    full_name: str


class MobileSessionInfo(BaseModel):
    id: UUID
    device_name: str
    platform: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    current: bool


class MobileIdentity(BaseModel):
    user: MobileUser
    session: MobileSessionInfo


class MobileTokens(MobileIdentity):
    token_type: Literal["Bearer"] = "Bearer"
    access_token: str
    refresh_token: str
    expires_in: int


class MobileCapabilities(BaseModel):
    email_delivery_available: bool
    minimum_password_length: int = 10


class MobileAccepted(BaseModel):
    status: Literal["accepted"] = "accepted"


class MobileFailure(BaseModel):
    code: Literal[
        "invalid_credentials",
        "invalid_session",
        "invalid_code",
        "invalid_request",
        "email_unavailable",
        "rate_limited",
        "unavailable",
        "not_found",
    ]
