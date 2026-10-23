"""Post a digest to a webhook: Slack, Discord, ntfy, or plain JSON.

The URL is a secret (Slack and Discord URLs carry the token), so it is shown masked
and kept out of ``torus export``. One attempt, 10 s timeout; the next scheduled digest
is the retry.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from torus.core.digest import Digest, to_text, truncate_markdown

FORMATS = ("auto", "slack", "discord", "ntfy", "json")
LIMITS = {"slack": 3500, "discord": 1900, "ntfy": 3500}  # characters, under each service's cap
TIMEOUT = 10.0
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

Poster = Callable[[urllib.request.Request, float], int]


class WebhookError(Exception):
    pass


def check_url(url: str) -> str | None:
    """An error message if ``url`` can't be a webhook (https, or http on this machine)."""
    parts = urlsplit(url.strip())
    if parts.scheme == "https" and parts.hostname:
        return None
    if parts.scheme == "http" and parts.hostname in LOCAL_HOSTS:
        return None
    return "digest_webhook must be an https:// URL (http:// only for localhost)."


def mask_url(url: str) -> str:
    """``https://hooks.slack.com/.../AbCd``: enough to recognise, not enough to use."""
    parts = urlsplit(url)
    tail = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return f"{parts.scheme}://{parts.hostname}/.../{tail[-4:]}" if tail else url


def resolve_format(url: str, fmt: str) -> str:
    if fmt != "auto":
        return fmt
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host == "hooks.slack.com":
        return "slack"
    if host in ("discord.com", "discordapp.com") and parts.path.startswith("/api/webhooks/"):
        return "discord"
    if host == "ntfy.sh":
        return "ntfy"
    return "json"


def build_request(
    url: str, fmt: str, d: Digest, full_at: str | None = None
) -> urllib.request.Request:
    fmt = resolve_format(url, fmt)
    note = f"(Shortened; the full digest is at {full_at}.)" if full_at else "(Shortened.)"
    headers = {"User-Agent": "torus"}
    body: bytes
    if fmt == "json":
        payload: Any = d.model_dump(mode="json")
        body, headers["Content-Type"] = json.dumps(payload).encode(), "application/json"
    elif fmt == "ntfy":
        body = truncate_markdown(d.markdown, LIMITS["ntfy"], note).encode()
        headers |= {
            "Title": f"Torus digest {d.until:%Y-%m-%d}",
            "Markdown": "yes",
            "Content-Type": "text/markdown; charset=utf-8",
        }
        if any(a.strength == "strong" for g in d.new_alerts for a in g.alerts):
            headers["Priority"] = "high"
    else:
        text = truncate_markdown(d.markdown, LIMITS[fmt], note)
        payload = {"text": to_text(text)} if fmt == "slack" else {"content": text}
        body, headers["Content-Type"] = json.dumps(payload).encode(), "application/json"
    return urllib.request.Request(url, data=body, headers=headers, method="POST")


def _post(request: urllib.request.Request, timeout: float) -> int:
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        status: int = resp.status
        return status


def send(url: str, fmt: str, d: Digest, full_at: str | None = None, poster: Poster = _post) -> None:
    request = build_request(url, fmt, d, full_at)
    try:
        status = poster(request, TIMEOUT)
    except urllib.error.HTTPError as exc:
        detail = exc.read(200).decode("utf-8", "replace").strip()
        raise WebhookError(f"{mask_url(url)}: HTTP {exc.code} {detail}".strip()) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise WebhookError(f"{mask_url(url)}: {exc}") from exc
    if not 200 <= status < 300:
        raise WebhookError(f"{mask_url(url)}: HTTP {status}")
