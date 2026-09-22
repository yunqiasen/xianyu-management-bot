"""Typed administrator settings, separate from platform account recovery limits."""
from pydantic import BaseModel, ConfigDict, Field


class LoginProtectionSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    window_seconds: int = Field(default=900, ge=60, le=86400, strict=True)
    lock_seconds: int = Field(default=900, ge=60, le=86400, strict=True)
    username_threshold: int = Field(default=5, ge=1, le=100, strict=True)
    ip_threshold: int = Field(default=20, ge=1, le=1000, strict=True)


class LoginProtectionUpdate(LoginProtectionSettings):
    expected_version: int = Field(ge=0, strict=True)
