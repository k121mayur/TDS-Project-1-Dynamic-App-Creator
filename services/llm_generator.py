"""LLM-backed generator that produces static web app code for task briefs."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, List, Optional

from openai import OpenAI, OpenAIError

from config import Settings
from schemas import TaskRequest

logger = logging.getLogger("orchestrator.llm")

MAX_FILE_BYTES = 512_000  # 512 KB per generated file cap


class LLMGenerationError(RuntimeError):
    """Raised when the LLM is unable to produce source files."""


def _strip_code_fence(payload: str) -> str:
    """Remove Markdown code fences if present to recover raw JSON."""

    text = payload.strip()
    if text.startswith("```"):
        parts = text.split("```", maxsplit=2)
        if len(parts) >= 2:
            return parts[1].lstrip("json").strip()
    return text


def _sanitise_path(path: str) -> str:
    """Ensure generated paths stay within the repository."""

    candidate = path.strip()
    candidate = candidate.lstrip("./")
    pure = PurePosixPath(candidate)
    if not candidate:
        raise LLMGenerationError("Generated file path is empty.")
    if any(part in {"..", ""} for part in pure.parts):
        raise LLMGenerationError(f"Unsafe path produced by LLM: {path!r}")
    return pure.as_posix()


@dataclass
class LLMGenerationResult:
    files: Dict[str, bytes]
    raw_response: str
    model: str


class LLMGenerator:
    """High-level façade around the OpenAI chat completion API."""

    def __init__(self, settings: Settings) -> None:
        auth_token = settings.openai_api_key or settings.ai_pipe_token
        if not auth_token:
            raise LLMGenerationError(
                "Neither OPENAI_API_KEY nor AI_PIPE_TOKEN is configured.",
            )

        client_kwargs: Dict[str, object] = {"api_key": auth_token}
        if settings.openai_base_url:
            client_kwargs["base_url"] = settings.openai_base_url

        if settings.ai_pipe_token and not settings.openai_api_key:
            default_headers = {
                "Authorization": f"Bearer {settings.ai_pipe_token}",
                "X-API-Key": settings.ai_pipe_token,
            }
            client_kwargs["default_headers"] = default_headers

        self.client = OpenAI(**client_kwargs)
        self.model = settings.openai_model
        self.temperature = settings.openai_temperature
        self.last_raw_response: Optional[str] = None

    def _build_messages(
        self,
        task: TaskRequest,
        attachment_summaries: List[str],
    ) -> List[dict]:
        attachments_text = (
            "\n".join(f"- {summary}" for summary in attachment_summaries)
            if attachment_summaries
            else "No attachments were supplied."
        )

        checks_text = (
            "\n".join(f"- {item}" for item in task.checks)
            if task.checks
            else "No automated checks provided."
        )

        system_message = (
            "You are an expert web developer who only delivers static websites "
            "(HTML, CSS, JS) without build steps. Always provide production-ready "
            "code that humans can deploy directly on GitHub Pages. "
            "Respond strictly in JSON matching the schema described by the user."
        )

        user_message = (
            f"Task brief:\n{task.brief}\n\n"
            f"Task metadata:\n"
            f"- Task ID: {task.task}\n"
            f"- Round: {task.round}\n"
            f"- Requester email: {task.email}\n"
            f"- Nonce: {task.nonce}\n\n"
            f"Evaluation checks to satisfy:\n{checks_text}\n\n"
            f"Attachments (available under ./assets/ in the repo):\n{attachments_text}\n\n"
            "Requirements:\n"
            "- Produce HTML, CSS, and JavaScript only. Avoid build tooling or package managers.\n"
            "- Include a README.md describing the app, setup instructions (if any), and how it satisfies the brief.\n"
            "- Include an MIT LICENSE file with a generic copyright notice.\n"
            "- Provide a .github/workflows/pages.yml workflow that deploys the static site.\n"
            "- Put all code in the repository root (except the workflow folder).\n"
            "- Reference attachments using relative paths (e.g., ./assets/<filename>).\n"
            "- Ensure the main page is index.html and include any additional assets (CSS, JS) as separate files.\n"
            "- Do not minify aggressively; prioritise readability and maintainability.\n\n"
            "Output schema (respond with JSON only, no prose):\n"
            "{\n"
            '  "files": [\n'
            "    {\n"
            '      "path": "index.html",\n'
            '      "content": "<!doctype html>...",\n'
            '      "encoding": "utf-8"  // or "base64" for binary assets\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Ensure all mandatory files (index.html, styles.css, script.js if needed, README.md, LICENSE, "
            "and .github/workflows/pages.yml) are present in the response."
        )

        return [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ]

    def _parse_files(self, payload: str) -> Dict[str, bytes]:
        cleaned = _strip_code_fence(payload)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise LLMGenerationError(f"LLM response is not valid JSON: {exc}") from exc

        files_field = parsed.get("files")
        if not isinstance(files_field, list) or not files_field:
            raise LLMGenerationError("LLM response did not contain any files.")

        result: Dict[str, bytes] = {}
        for entry in files_field:
            if not isinstance(entry, dict):
                raise LLMGenerationError("Each file entry must be a JSON object.")
            path = entry.get("path")
            content = entry.get("content")
            encoding = (entry.get("encoding") or "utf-8").lower()

            if not path or not isinstance(path, str):
                raise LLMGenerationError("A file entry is missing a string path.")
            if content is None or not isinstance(content, str):
                raise LLMGenerationError(f"File {path!r} is missing textual content.")

            safe_path = _sanitise_path(path)
            if encoding == "base64":
                try:
                    bytes_content = base64.b64decode(content)
                except Exception as exc:  # noqa: BLE001
                    raise LLMGenerationError(
                        f"Could not decode base64 content for {safe_path}: {exc}",
                    ) from exc
            else:
                bytes_content = content.encode("utf-8")

            if len(bytes_content) > MAX_FILE_BYTES:
                raise LLMGenerationError(
                    f"Generated file {safe_path} exceeds {MAX_FILE_BYTES} bytes.",
                )

            result[safe_path] = bytes_content

        return result

    def generate_app(
        self,
        task: TaskRequest,
        attachment_summaries: List[str],
    ) -> LLMGenerationResult:
        messages = self._build_messages(task, attachment_summaries)
        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                response_format={"type": "json_object"},
                messages=messages,
            )
        except OpenAIError as exc:
            raise LLMGenerationError(
                f"OpenAI API call failed: {exc}",
            ) from exc

        message = completion.choices[0].message
        if not message or not message.content:
            raise LLMGenerationError("LLM returned an empty response.")

        self.last_raw_response = message.content
        files = self._parse_files(message.content)
        logger.info(
            "Generated %s files via LLM model %s",
            len(files),
            self.model,
        )
        return LLMGenerationResult(
            files=files,
            raw_response=message.content,
            model=self.model,
        )
