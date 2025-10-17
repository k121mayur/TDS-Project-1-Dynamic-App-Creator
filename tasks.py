"""Celery worker tasks for orchestrating app generation and deployment."""

from __future__ import annotations

import json
from fastapi.encoders import jsonable_encoder
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, Optional
from uuid import uuid4

import httpx
from celery import Celery

from codegen import (
    generate_static_site,
    render_license,
    render_pages_workflow,
    render_readme,
)
from config import Settings, get_settings
from schemas import CallbackPayload, TaskRequest
from services.github_service import GitHubService, GitHubServiceError, RepoInfo
from services.llm_generator import LLMGenerationError, LLMGenerator
from utils import build_pages_url, slugify, write_attachments

settings: Settings = get_settings()

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger("orchestrator.worker")


celery_app = Celery(
    "tds_orchestrator",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
)


def _compose_repo_name(task_request: TaskRequest) -> str:
    slug = slugify(task_request.task)
    suffix = uuid4().hex[:6]
    return f"{slug}-r{task_request.round}-{suffix}"


def _persist_local_repo(files: Dict[str, bytes], repo_name: str, owner: str) -> RepoInfo:
    output_root = Path("artifacts") / repo_name
    for path, content in files.items():
        target = output_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    pages_url = build_pages_url(owner, repo_name)
    return RepoInfo(
        owner=owner,
        name=repo_name,
        html_url=f"https://example.com/{repo_name}",
        default_branch=settings.github_default_branch,
        pages_url=pages_url,
    )


def _wait_for_pages(url: str, timeout: int, interval: int) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=10.0)
            if response.status_code == 200:
                return "ready"
        except httpx.HTTPError:
            pass

        time.sleep(interval)
    return "pending"


@celery_app.task(name="orchestrate_task")
def orchestrate_task(payload: dict) -> None:
    """
    Generate app, push to GitHub or local filesystem, then notify evaluator.
    """

    task_payload = {k: v for k, v in payload.items() if not k.startswith("_")}
    task_request = TaskRequest(**task_payload)
    logger.info("Worker received task %s", task_request.task)

    repo_name = _compose_repo_name(task_request)
    owner = settings.github_owner or "example"
    github_service: Optional[GitHubService] = None

    if not settings.dry_run and settings.github_token:
        try:
            github_service = GitHubService(settings)
            owner = settings.github_owner or github_service.login
        except GitHubServiceError as exc:
            logger.error("GitHub configuration error: %s", exc)
            return

    pages_url = build_pages_url(owner, repo_name)

    try:
        with TemporaryDirectory(prefix="tds-orchestrator-") as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            repo_root.mkdir(parents=True, exist_ok=True)

            attachment_paths = write_attachments(
                task_request.attachments,
                repo_root / "assets",
            )

            attachment_summaries: list[str] = []
            for relative_path in attachment_paths:
                absolute = repo_root / relative_path
                try:
                    size = absolute.stat().st_size
                except FileNotFoundError:
                    size = 0
                attachment_summaries.append(f"{relative_path} ({size} bytes)")

            llm_result = None
            if settings.openai_api_key or settings.ai_pipe_token:
                try:
                    generator = LLMGenerator(settings)
                    llm_result = generator.generate_app(task_request, attachment_summaries)
                except LLMGenerationError as exc:
                    logger.warning("LLM generation failed: %s", exc)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Unhandled LLM error: %s", exc)
            else:
                logger.warning(
                    "No LLM credentials configured; falling back to deterministic template for task %s",
                    task_request.task,
                )

            files_to_publish: Dict[str, bytes]

            if llm_result:
                files_to_publish = dict(llm_result.files)
            else:
                fallback_site = generate_static_site(
                    task_request,
                    attachment_paths,
                    pages_url,
                    license_holder=owner,
                )
                files_to_publish = dict(fallback_site)

            if llm_result:
                logger.info(
                    "LLM generated %s files for task %s",
                    len(files_to_publish),
                    task_request.task,
                )
            else:
                logger.warning(
                    "Falling back to deterministic template for task %s",
                    task_request.task,
                )

            if llm_result:
                files_to_publish.setdefault(
                    "automation/llm_raw_output.json",
                    llm_result.raw_response.encode("utf-8"),
                )

            required_defaults = {
                "LICENSE": render_license(owner).encode("utf-8"),
                ".github/workflows/pages.yml": render_pages_workflow().encode("utf-8"),
                "README.md": render_readme(task_request, pages_url).encode("utf-8"),
            }
            for path, content in required_defaults.items():
                files_to_publish.setdefault(path, content)

            if "index.html" not in files_to_publish:
                logger.warning(
                    "Primary HTML asset missing from LLM output; supplementing with fallback template.",
                )
                supplemental = generate_static_site(
                    task_request,
                    attachment_paths,
                    pages_url,
                    license_holder=owner,
                )
                for key, value in supplemental.items():
                    files_to_publish.setdefault(key, value)

            for relative_path in attachment_paths:
                absolute = repo_root / relative_path
                files_to_publish[relative_path] = absolute.read_bytes()

            files_to_publish["task.json"] = json.dumps(
                jsonable_encoder(task_request),
                indent=2,
            ).encode("utf-8")

            commit_sha = uuid4().hex
            repo_info: RepoInfo
            pages_status = "pending"

            if github_service:
                with github_service:
                    repo_info = github_service.ensure_repo(
                        repo_name=repo_name,
                        description=f"Automated deliverable for {task_request.task}",
                        homepage=pages_url,
                        topics=["tds", "automation", "llm"],
                    )
                    commit_sha = github_service.push_files(
                        repo_info,
                        files_to_publish,
                        commit_message=f"Initial commit for {task_request.task}",
                        branch=settings.github_default_branch,
                    )

                    try:
                        github_service.ensure_pages_enabled(
                            repo_info,
                            branch=settings.github_default_branch,
                        )
                    except httpx.HTTPStatusError as exc:
                        logger.warning("Unable to enable GitHub Pages: %s", exc)

                    pages_url = build_pages_url(repo_info.owner, repo_info.name)
                    pages_status = _wait_for_pages(
                        pages_url,
                        timeout=settings.pages_timeout_seconds,
                        interval=settings.pages_poll_interval,
                    )
            else:
                repo_info = _persist_local_repo(files_to_publish, repo_name, owner)
                pages_url = repo_info.pages_url or pages_url
                pages_status = "dry-run"

            callback_payload = {
                "email": task_request.email,
                "task": task_request.task,
                "round": task_request.round,
                "nonce": task_request.nonce,
                "repo_url": repo_info.html_url,
                "commit_sha": commit_sha,
                "pages_url": pages_url,
                "pages_status": pages_status,
            }

            callback = CallbackPayload(**callback_payload)

            notify_payload = jsonable_encoder(
                callback,
                exclude_none=True,
            )

            attempt = 0
            delay = 1
            max_attempts = 5
            while attempt < max_attempts:
                attempt += 1
                try:
                    response = httpx.post(
                        str(task_request.evaluation_url),
                        json=notify_payload,
                        timeout=settings.callback_timeout_seconds,
                    )
                    response.raise_for_status()
                    logger.info(
                        "Callback delivered for %s task %s",
                        task_request.email,
                        task_request.task,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Callback attempt %s/%s failed: %s",
                        attempt,
                        max_attempts,
                        exc,
                    )
                    time.sleep(delay)
                    delay *= 2
            else:
                logger.error(
                    "Unable to notify evaluation URL %s after %s attempts",
                    task_request.evaluation_url,
                    max_attempts,
                )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error while processing task: %s", exc)





