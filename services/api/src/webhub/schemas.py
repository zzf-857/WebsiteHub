from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    status: Literal["ready"]
    checks: dict[str, Literal["ok"]]
