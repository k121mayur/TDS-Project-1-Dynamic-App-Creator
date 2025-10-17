"""Pydantic request/response models used by the orchestrator service."""

from datetime import datetime
from typing import List, Optional

from pydantic import AnyHttpUrl, BaseModel, EmailStr, Field, HttpUrl


class Attachment(BaseModel):
    """Represents a file included via data URI."""

    name: str = Field(..., min_length=1)
    url: AnyHttpUrl | str = Field(
        ...,
        description="Data URI or absolute URL pointing to the attachment contents.",
    )


class TaskRequest(BaseModel):
    """Payload accepted at POST /app to kick off orchestration."""

    email: EmailStr
    secret: str = Field(..., min_length=1)
    task: str = Field(..., min_length=1)
    round: int = Field(..., ge=1)
    nonce: str = Field(..., min_length=1)
    brief: str = Field(..., min_length=1)
    checks: List[str] = Field(default_factory=list)
    evaluation_url: HttpUrl
    attachments: List[Attachment] = Field(default_factory=list)


class AckResponse(BaseModel):
    """Synchronous acknowledgement response."""

    ok: bool = True
    received_at: datetime


class CallbackPayload(BaseModel):
    """Body sent to the external evaluation URL."""

    email: EmailStr
    task: str
    round: int
    nonce: str
    repo_url: HttpUrl
    commit_sha: str
    pages_url: HttpUrl
    pages_status: Optional[str] = Field(
        default=None,
        description="Optional status description for GitHub Pages readiness.",
    )

