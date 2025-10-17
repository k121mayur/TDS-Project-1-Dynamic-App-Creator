"""FastAPI entrypoint for the LLM Code Deployment orchestrator."""

from __future__ import annotations

from datetime import datetime, timezone
import logging

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from config import Settings, get_settings
from fastapi.encoders import jsonable_encoder
from schemas import AckResponse, TaskRequest
from tasks import orchestrate_task

logger = logging.getLogger("orchestrator.api")

app = FastAPI(
    title="TDS LLM Code Deployment Orchestrator",
    version="0.1.0",
    description="Receives task briefs and asynchronously builds + deploys static web apps.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


def validate_secret(task: TaskRequest, settings: Settings) -> None:
    """
    Ensure the incoming request secret matches the configured shared secret.

    When ``settings.app_secret`` is not set or ``settings.dry_run`` is true, the
    validation is skipped to allow local experimentation.
    """

    if settings.dry_run:
        return

    if not settings.app_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server secret not configured.",
        )

    if task.secret != settings.app_secret:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid secret provided.",
        )


@app.post("/app", response_model=AckResponse, status_code=status.HTTP_200_OK)
def receive_task(
    task_request: TaskRequest,
    settings: Settings = Depends(get_settings),
) -> AckResponse:
    """
    Accept a task brief, enqueue asynchronous orchestration, and respond immediately.
    """

    validate_secret(task_request, settings)

    payload = jsonable_encoder(task_request)
    payload["_received_at"] = datetime.now(timezone.utc).isoformat()
    orchestrate_task.delay(payload)

    logger.info(
        "Task accepted",
        extra={
            "task": task_request.task,
            "round": task_request.round,
            "email": task_request.email,
        },
    )

    return AckResponse(received_at=datetime.now(timezone.utc))


@app.get("/healthz", tags=["health"])
def health_check() -> dict[str, str]:
    """Simple liveness check."""

    return {"status": "ok"}




