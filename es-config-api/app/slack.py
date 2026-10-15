"""Slack messages through an incoming webhook.

The webhook URL is a secret: it lives in AWS Secrets Manager (``<prefix>slack``, a JSON object
``{"webhookUrl": ...}``), set from Admin → Alerts. Never in .env, S3 or the audit log. No
webhook = nothing is posted; alerts are still tracked and shown in the console.

Messages never carry document values or secrets: who, what, where, numbers like "83% used",
and a link to the console.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .errors import bad_request

log = logging.getLogger("es_config_api.slack")

SECRET_NAME = "slack"
WEBHOOK_RE = re.compile(r"^https://hooks\.slack\.com/(services|workflows|triggers)/[A-Za-z0-9_/\-]{10,300}$")

COLORS = {"critical": "#d1242f", "warning": "#d97706", "ok": "#1a7f37", "info": "#0969da"}


def esc(text: Any) -> str:
    """Escape text for Slack mrkdwn (&, <, > are control characters)."""
    return str(text if text is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def link(url: str | None, label: str) -> str:
    return f"<{url}|{esc(label)}>" if url else ""


def _http_post(url: str, payload: dict, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https host)
            return resp.status, resp.read(500).decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(500).decode(errors="replace")


class SlackNotifier:
    """Posts to the webhook in Secrets Manager. ``transport`` is replaceable for tests."""

    def __init__(self, secrets, timeout: float = 10.0,
                 transport: Callable[[str, dict, float], tuple[int, str]] | None = None,
                 async_send: bool = True):
        self.secrets = secrets
        self.timeout = timeout
        self.transport = transport or _http_post
        self.async_send = async_send
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self.last_error: dict | None = None
        self._fresh_at = 0.0

    # -- webhook (secret)
    def webhook(self) -> str | None:
        if self.secrets is None:
            return None
        # The secret store caches for minutes per worker; re-read at most every 30 s so a webhook
        # replaced or removed on one worker takes effect on the others quickly.
        cache = getattr(self.secrets, "cache", None)
        if cache is not None and time.monotonic() - self._fresh_at > 30:
            cache.drop(self.secrets.full_name(SECRET_NAME))
            self._fresh_at = time.monotonic()
        try:
            return (self.secrets.get(SECRET_NAME) or {}).get("webhookUrl") or None
        except Exception as e:  # unreadable secret = not configured; reported in the console
            log.warning("could not read the Slack webhook secret: %s", e)
            self.last_error = {"error": f"Could not read the Slack webhook from Secrets Manager: {e}"}
            return None

    @property
    def configured(self) -> bool:
        return bool(self.webhook())

    def set_webhook(self, url: str) -> None:
        url = (url or "").strip()
        if not WEBHOOK_RE.match(url):
            raise bad_request("INVALID_WEBHOOK", "Paste a Slack incoming-webhook URL that starts with "
                                                 "https://hooks.slack.com/services/")
        self.secrets.put(SECRET_NAME, {"webhookUrl": url}, "Slack incoming webhook for ES Config API alerts")
        self.last_error = None

    def remove_webhook(self) -> None:
        self.secrets.delete(SECRET_NAME)

    # -- send
    def post(self, text: str, attachments: list[dict] | None = None) -> dict:
        """Send now. Never raises: returns {"ok": bool, "error"?: str}."""
        url = self.webhook()
        if not url:
            return {"ok": False, "error": "Slack is not set up (Admin → Alerts)", "skipped": True}
        payload: dict = {"text": text}
        if attachments:
            payload["attachments"] = attachments
        try:
            status, body = self.transport(url, payload, self.timeout)
        except Exception as e:
            status, body = 0, str(e)
        if status == 200:
            self.last_error = None
            return {"ok": True}
        hint = {403: "the webhook was revoked or the app removed from the channel",
                404: "the webhook no longer exists (create a new one in Slack)",
                410: "the channel was archived or deleted",
                0: "could not reach hooks.slack.com (does the server allow outbound HTTPS?)"}.get(status, "")
        err = f"Slack answered {status}: {body[:200]}" + (f" ({hint})" if hint else "")
        self.last_error = {"error": err, "status": status}
        log.warning("Slack post failed: %s", err)
        return {"ok": False, "error": err}

    def post_async(self, text: str, attachments: list[dict] | None = None) -> None:
        """Fire and forget (used from request handlers so a slow Slack never delays them)."""
        if not self.async_send:
            self.post(text, attachments)
            return
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(1, thread_name_prefix="slack")
        self._pool.submit(self.post, text, attachments)


class RecordingTransport:
    """Test double: records what would have been posted."""

    def __init__(self, status: int = 200):
        self.status = status
        self.sent: list[dict] = []

    def __call__(self, url: str, payload: dict, timeout: float) -> tuple[int, str]:
        self.sent.append(payload)
        return self.status, "ok" if self.status == 200 else "error"

    def texts(self) -> list[str]:
        out = []
        for p in self.sent:
            out.append(p["text"] + "\n" + "\n".join(a.get("text", "") for a in p.get("attachments", [])))
        return out
