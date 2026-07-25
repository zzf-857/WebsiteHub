from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterRequest(StrictRequest):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, min_length=1, max_length=80)


class LoginRequest(StrictRequest):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordRequest(StrictRequest):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class PreferenceUpdateRequest(StrictRequest):
    theme: Literal["system", "light", "dark"]


class PreferenceResponse(BaseModel):
    theme: Literal["system", "light", "dark"]
    locale: str


class UserResponse(BaseModel):
    id: str
    username: str
    display_name: str
    created_at: datetime
    preferences: PreferenceResponse


class AuthResponse(BaseModel):
    user: UserResponse


class MessageResponse(BaseModel):
    message: str
