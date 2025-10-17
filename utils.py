"""Utility helpers for slug creation, attachment parsing, and filesystem writes."""

from __future__ import annotations

import base64
import re
import unicodedata
from pathlib import Path
from typing import Iterable, Tuple
from urllib.parse import unquote_plus, urlparse

import httpx

from schemas import Attachment

DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[\w/+.-]+)?(?P<params>(;[\w=+-]+)*)?(;base64)?,(?P<data>.*)$",
    re.IGNORECASE,
)


def slugify(value: str, max_length: int = 63) -> str:
    """Return a filesystem-and-repo-friendly slug."""

    value = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    if not value:
        value = "generated-app"
    return value[:max_length]


def decode_data_uri(data_uri: str) -> Tuple[bytes, str | None]:
    """
    Decode a data URI into bytes and return optional MIME type.

    Raises ValueError if the URI is malformed.
    """

    match = DATA_URI_RE.match(data_uri)
    if not match:
        raise ValueError("Invalid data URI")

    mime = match.group("mime")
    payload = match.group("data") or ""
    is_base64 = ";base64" in (match.group("params") or "")
    if is_base64:
        return base64.b64decode(payload), mime
    return unquote_plus(payload).encode("utf-8"), mime


def safe_attachment_path(name: str) -> str:
    """Return a safe relative path for an attachment filename."""

    sanitized = re.sub(r"[^\w.\-]+", "_", name.strip())
    return sanitized or "attachment"


def write_attachments(attachments: Iterable[Attachment], target_dir: Path) -> list[str]:
    """
    Persist attachments into ``target_dir`` and return relative file paths.

    Supports both data URIs and HTTP(S) URLs.
    """

    saved_paths: list[str] = []
    target_dir.mkdir(parents=True, exist_ok=True)

    for attachment in attachments:
        destination = target_dir / safe_attachment_path(attachment.name or "attachment")
        source_url = str(attachment.url)
        try:
            if source_url.startswith("data:"):
                payload, _ = decode_data_uri(source_url)
            else:
                response = httpx.get(source_url, timeout=30.0)
                response.raise_for_status()
                payload = response.content
        except Exception as exc:  # noqa: BLE001
            destination = target_dir / f"failed-{safe_attachment_path(attachment.name)}.txt"
            destination.write_text(
                f"Attachment {attachment.name!r} could not be fetched: {exc}\n",
                encoding="utf-8",
            )
            saved_paths.append(destination.relative_to(target_dir.parent).as_posix())
            continue

        destination.write_bytes(payload)
        saved_paths.append(destination.relative_to(target_dir.parent).as_posix())

    return saved_paths


def build_pages_url(owner: str, repo_name: str) -> str:
    """Construct the canonical GitHub Pages URL for the repo."""

    owner = owner.strip("/")
    repo_name = repo_name.strip("/")
    return f"https://{owner}.github.io/{repo_name}/"


def is_http_url(value: str) -> bool:
    """Return True if the value looks like an HTTP(S) URL."""

    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

